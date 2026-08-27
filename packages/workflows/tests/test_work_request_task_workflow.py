"""WorkRequestTaskWorkflow orchestration with a stub child + activity, and
AgentTaskWorkflow starting it for accepted requests (idempotently)."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.agent_task import (
    ACTIVITY_RESOLVE_SNAPSHOT,
    AgentTaskInput,
    AgentTaskResult,
    AgentTaskWorkflow,
    FinalizeInput,
    SnapshotResult,
    StepResult,
)
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    AdvertisedTool,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    WorkRequestStart,
)
from jhin_workflows.work_request_task import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    FinalizeWorkRequestInput,
    WorkRequestTaskInput,
    WorkRequestTaskResult,
    WorkRequestTaskWorkflow,
    work_request_workflow_id,
)


@workflow.defn(name="AgentTaskWorkflow")
class StubAgentTaskWorkflow:
    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        if params.task_id.startswith("h"):
            # Never finishes: stands in for a run parked on an approval
            # nobody decides. The parent's execution timeout must end it.
            await workflow.wait_condition(lambda: False)
        status = "failed" if params.task_id.startswith("f") else "completed"
        return AgentTaskResult(run_id=f"run-{params.task_id}", status=status, steps_used=1)


class Stubs:
    def __init__(self) -> None:
        self.finalized: list[FinalizeWorkRequestInput] = []

    @activity.defn(name=ACTIVITY_FINALIZE_WORK_REQUEST)
    async def finalize(self, params: FinalizeWorkRequestInput) -> str:
        self.finalized.append(params)
        return "completed" if params.run_status == "completed" else "failed"


async def run_workflow(stubs: Stubs, params: WorkRequestTaskInput) -> Any:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[WorkRequestTaskWorkflow, StubAgentTaskWorkflow],
            activities=[stubs.finalize],
        ):
            return await env.client.execute_workflow(
                WorkRequestTaskWorkflow.run,
                params,
                id=work_request_workflow_id(params.work_request_id),
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


async def test_completed_task_finalizes_request() -> None:
    stubs = Stubs()
    params = WorkRequestTaskInput(
        workspace_id=str(uuid.uuid4()),
        work_request_id=str(uuid.uuid4()),
        task_id="c" + str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    result: WorkRequestTaskResult = await run_workflow(stubs, params)
    assert result.run_status == "completed" and result.request_status == "completed"
    assert [f.work_request_id for f in stubs.finalized] == [params.work_request_id]


async def test_failed_task_still_finalizes() -> None:
    stubs = Stubs()
    params = WorkRequestTaskInput(
        workspace_id=str(uuid.uuid4()),
        work_request_id=str(uuid.uuid4()),
        task_id="f" + str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    result: WorkRequestTaskResult = await run_workflow(stubs, params)
    assert result.run_status == "failed" and result.request_status == "failed"


async def test_hung_task_is_time_boxed_and_finalized() -> None:
    """A request a colleague never finishes still reaches a terminal state
    with a reason, instead of holding one of their slots forever."""
    stubs = Stubs()
    params = WorkRequestTaskInput(
        workspace_id=str(uuid.uuid4()),
        work_request_id=str(uuid.uuid4()),
        task_id="h" + str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    result: WorkRequestTaskResult = await run_workflow(stubs, params)
    assert result.run_status == "timed_out"
    assert result.request_status == "failed"
    assert [f.run_status for f in stubs.finalized] == ["timed_out"]


# --- AgentTaskWorkflow starts the child for accepted requests ---


@workflow.defn(name="WorkRequestTaskWorkflow")
class StubWorkRequestTaskWorkflow:
    @workflow.run
    async def run(self, params: WorkRequestTaskInput) -> WorkRequestTaskResult:
        return WorkRequestTaskResult(
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status="completed",
            request_status="completed",
        )


class AgentStubs:
    """Phase 10 step path (docs/architecture/tool-worker-boundary.md): the
    tool queue resolves tools and cleans up, the agent queue reasons,
    commits, and finalizes. Each commit reports the same accepted request."""

    def __init__(self, starts: list[WorkRequestStart]) -> None:
        self.starts = starts
        self.steps = 0

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve(self, params: AgentTaskInput) -> SnapshotResult:
        return SnapshotResult(run_id="run-1", snapshot_json="{}", snapshot_hash="h", max_steps=5)

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def advertised(self, params: ResolveAdvertisedToolsInput) -> list[AdvertisedTool]:
        return []

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason(self, params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        return ReasonAgentStepResult(call_count=0)

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit(self, params: CommitAgentStepInput) -> StepResult:
        self.steps += 1
        # Two steps report the same accepted request (a retried tool call):
        # the second start must be a no-op, not a failure.
        return StepResult(done=self.steps >= 2, work_request_starts=list(self.starts))

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup(self, params: CleanupRunWorkspaceInput) -> CleanupRunWorkspaceResult:
        return CleanupRunWorkspaceResult(deleted=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize(self, params: FinalizeInput) -> None:
        return None


async def test_agent_task_workflow_starts_work_request_child_once() -> None:
    request_id = str(uuid.uuid4())
    starts = [WorkRequestStart(work_request_id=request_id, task_id="t1", agent_id="a2")]
    stubs = AgentStubs(starts)
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow, StubWorkRequestTaskWorkflow],
                activities=[stubs.resolve, stubs.reason, stubs.commit, stubs.finalize],
            ),
            Worker(
                env.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=[stubs.advertised, stubs.cleanup],
            ),
        ):
            result: AgentTaskResult = await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                AgentTaskInput(workspace_id="ws", task_id="parent", agent_id="a1"),
                id=f"task-{uuid.uuid4()}",
                task_queue=AGENT_TASK_QUEUE,
            )
            assert result.status == "completed"
            assert stubs.steps == 2
            handle = env.client.get_workflow_handle(
                work_request_workflow_id(request_id), result_type=WorkRequestTaskResult
            )
            child: WorkRequestTaskResult = await handle.result()
            assert child.work_request_id == request_id and child.task_id == "t1"
    finally:
        await env.shutdown()
