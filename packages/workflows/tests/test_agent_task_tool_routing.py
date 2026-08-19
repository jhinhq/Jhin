"""Two-queue orchestration coverage for the Phase 10 agent-task route."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_COMMIT_APPROVAL_PROJECTION,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_APPROVAL,
    ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    CommitApprovalProjectionInput,
    ExecuteBoundToolInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    ResolveApprovalInput,
    ResolveBoundToolApprovalInput,
    RunStepInput,
    SnapshotResult,
    StepResult,
)

_APPROVAL_ID = "018f4d52-8b93-7d41-8ac7-7f190f092222"
_TOOL_CALL_ID = "018f4d52-8b93-7d41-8ac7-7f190f093333"
_EFFECT_ACTIVITIES = frozenset(
    {
        ACTIVITY_EXECUTE_BOUND_TOOL,
        ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL,
        ACTIVITY_CLEANUP_RUN_WORKSPACE,
    }
)


class TwoQueueWorld:
    """Runs the real workflow against deterministic activity boundaries."""

    def __init__(self) -> None:
        self.agent_queue = AGENT_TASK_QUEUE
        self.activity_calls: list[tuple[str, str]] = []
        self.executed_ordinals: list[int] = []
        self.commit_calls: list[CommitAgentStepInput] = []
        self.finalize_calls: list[FinalizeInput] = []
        self.approval_projection_calls: list[CommitApprovalProjectionInput] = []
        self.run_id = str(uuid.uuid4())
        self.params = AgentTaskInput(
            workspace_id=str(uuid.uuid4()),
            task_id=str(uuid.uuid4()),
            agent_id=str(uuid.uuid4()),
        )
        self._scenario = "one_step"
        self._reason_calls = 0
        self._approval_parked = asyncio.Event()
        self._first_execute_started = asyncio.Event()
        self._release_first_execute = asyncio.Event()
        self._commit_started = asyncio.Event()
        self._release_commit = asyncio.Event()

    @property
    def effect_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.activity_calls if call[0] in _EFFECT_ACTIVITIES]

    @property
    def effect_activity_names(self) -> list[str]:
        return [name for name, _queue in self.effect_calls]

    def expected_prefix(self, scenario: str) -> list[int]:
        return {
            "zero_calls": [],
            "approval": [0],
            "blocking_delegation": [0],
            "execution_unknown": [0],
            "cancellation": [0],
            "cleanup_failure": [],
        }[scenario]

    def _record(self, name: str) -> None:
        self.activity_calls.append((name, activity.info().task_queue))

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot(self, _params: AgentTaskInput) -> SnapshotResult:
        self._record(ACTIVITY_RESOLVE_SNAPSHOT)
        return SnapshotResult(
            run_id=self.run_id,
            snapshot_json="{}",
            snapshot_hash="snapshot-hash",
            max_steps=3,
        )

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools(
        self, _params: ResolveAdvertisedToolsInput
    ) -> list[AdvertisedTool]:
        self._record(ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
        return [
            AdvertisedTool(
                name="system.echo",
                description="Echo one value",
                parameters={"type": "object"},
            )
        ]

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step(self, _params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        self._record(ACTIVITY_REASON_AGENT_STEP)
        self._reason_calls += 1
        if self._reason_calls > 1 or self._scenario in {
            "zero_calls",
            "cleanup_failure",
            "cleanup_no_poller",
        }:
            return ReasonAgentStepResult(call_count=0)
        if self._scenario == "one_step":
            return ReasonAgentStepResult(call_count=1)
        return ReasonAgentStepResult(call_count=3)

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool(self, params: ExecuteBoundToolInput) -> BoundToolResult:
        self._record(ACTIVITY_EXECUTE_BOUND_TOOL)
        self.executed_ordinals.append(params.ordinal)
        if self._scenario == "cancellation" and params.ordinal == 0:
            self._first_execute_started.set()
            await self._release_first_execute.wait()
        if self._scenario == "approval":
            return BoundToolResult(
                tool_call_id=_TOOL_CALL_ID,
                status="needs_approval",
                approval_id=_APPROVAL_ID,
                stop_reason="needs_approval",
            )
        if self._scenario == "blocking_delegation":
            return BoundToolResult(
                tool_call_id=_TOOL_CALL_ID,
                status="executed",
                stop_reason="blocking_delegation",
            )
        if self._scenario == "execution_unknown":
            return BoundToolResult(
                tool_call_id=_TOOL_CALL_ID,
                status="execution_unknown",
                stop_reason="execution_unknown",
            )
        return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit_agent_step(self, params: CommitAgentStepInput) -> StepResult:
        self._record(ACTIVITY_COMMIT_AGENT_STEP)
        self.commit_calls.append(params)
        if self._scenario == "cleanup_no_poller":
            self._commit_started.set()
            await self._release_commit.wait()
            return StepResult(done=True)
        if self._scenario == "execution_unknown":
            raise ApplicationError(
                "tool execution outcome is unknown",
                type="tool_execution_unknown",
                non_retryable=True,
            )
        if self._scenario == "approval" and params.step_index == 0:
            self._approval_parked.set()
            return StepResult(done=False, waiting_approval_id=_APPROVAL_ID)
        if self._scenario == "cancellation":
            return StepResult(done=False)
        return StepResult(done=True)

    @activity.defn(name=ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
    async def resolve_bound_tool_approval(
        self, _params: ResolveBoundToolApprovalInput
    ) -> BoundToolResult:
        self._record(ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL)
        return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_COMMIT_APPROVAL_PROJECTION)
    async def commit_approval_projection(self, params: CommitApprovalProjectionInput) -> StepResult:
        self._record(ACTIVITY_COMMIT_APPROVAL_PROJECTION)
        self.approval_projection_calls.append(params)
        return StepResult(done=False)

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace(
        self, _params: CleanupRunWorkspaceInput
    ) -> CleanupRunWorkspaceResult:
        self._record(ACTIVITY_CLEANUP_RUN_WORKSPACE)
        if self._scenario == "cleanup_failure":
            raise ApplicationError(
                "injected cleanup failure",
                type="cleanup_failed",
                non_retryable=True,
            )
        return CleanupRunWorkspaceResult(deleted=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize_run_projection(self, params: FinalizeInput) -> None:
        self._record(ACTIVITY_FINALIZE_RUN_PROJECTION)
        self.finalize_calls.append(params)

    # Register the Phase 9 activities as sentinels: before production changes,
    # the new-history tests finish quickly and show the exact legacy route.
    @activity.defn(name=ACTIVITY_RUN_AGENT_STEP)
    async def run_agent_step(self, _params: RunStepInput) -> StepResult:
        self._record(ACTIVITY_RUN_AGENT_STEP)
        return StepResult(done=True)

    @activity.defn(name=ACTIVITY_RESOLVE_APPROVAL)
    async def resolve_approval(self, _params: ResolveApprovalInput) -> StepResult:
        self._record(ACTIVITY_RESOLVE_APPROVAL)
        return StepResult(done=False)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN)
    async def finalize_run(self, params: FinalizeInput) -> None:
        self._record(ACTIVITY_FINALIZE_RUN)
        self.finalize_calls.append(params)

    async def run_one_step(self) -> Any:
        return await self.run_scenario("one_step")

    async def run_scenario(self, scenario: str) -> Any:
        self._scenario = scenario
        environment = await WorkflowEnvironment.start_time_skipping()
        try:
            async with (
                Worker(
                    environment.client,
                    task_queue=self.agent_queue,
                    workflows=[AgentTaskWorkflow],
                    activities=[
                        self.resolve_snapshot,
                        self.reason_agent_step,
                        self.commit_agent_step,
                        self.commit_approval_projection,
                        self.finalize_run_projection,
                        self.run_agent_step,
                        self.resolve_approval,
                        self.finalize_run,
                    ],
                ),
                Worker(
                    environment.client,
                    task_queue=TOOL_TASK_QUEUE,
                    activities=[
                        self.resolve_advertised_tools,
                        self.execute_bound_tool,
                        self.resolve_bound_tool_approval,
                        self.cleanup_run_workspace,
                    ],
                ),
            ):
                handle = await environment.client.start_workflow(
                    AgentTaskWorkflow.run,
                    self.params,
                    id=f"task-{self.params.task_id}",
                    task_queue=self.agent_queue,
                )
                result_task = asyncio.create_task(handle.result())
                if scenario == "approval":
                    parked_task = asyncio.create_task(self._approval_parked.wait())
                    done, _pending = await asyncio.wait(
                        {parked_task, result_task},
                        timeout=5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if result_task in done:
                        parked_task.cancel()
                        return result_task.result()
                    assert parked_task in done
                    await handle.signal(
                        "approval_decision",
                        args=[_APPROVAL_ID, "approved"],
                    )
                elif scenario == "cancellation":
                    started_task = asyncio.create_task(self._first_execute_started.wait())
                    done, _pending = await asyncio.wait(
                        {started_task, result_task},
                        timeout=5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if result_task in done:
                        started_task.cancel()
                        return result_task.result()
                    assert started_task in done
                    await handle.signal("cancel")
                    self._release_first_execute.set()
                return await result_task
        finally:
            self._release_first_execute.set()
            await environment.shutdown()

    async def run_without_cleanup_poller(self) -> Any:
        self._scenario = "cleanup_no_poller"
        environment = await WorkflowEnvironment.start_time_skipping()
        stop_tool_worker = asyncio.Event()
        tool_worker_ready = asyncio.Event()

        async def serve_tool_queue() -> None:
            async with Worker(
                environment.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=[
                    self.resolve_advertised_tools,
                    self.execute_bound_tool,
                    self.resolve_bound_tool_approval,
                    self.cleanup_run_workspace,
                ],
            ):
                tool_worker_ready.set()
                await stop_tool_worker.wait()

        tool_worker_task: asyncio.Task[None] | None = None
        try:
            async with Worker(
                environment.client,
                task_queue=self.agent_queue,
                workflows=[AgentTaskWorkflow],
                activities=[
                    self.resolve_snapshot,
                    self.reason_agent_step,
                    self.commit_agent_step,
                    self.commit_approval_projection,
                    self.finalize_run_projection,
                    self.run_agent_step,
                    self.resolve_approval,
                    self.finalize_run,
                ],
            ):
                tool_worker_task = asyncio.create_task(serve_tool_queue())
                await asyncio.wait_for(tool_worker_ready.wait(), timeout=5)
                handle = await environment.client.start_workflow(
                    AgentTaskWorkflow.run,
                    self.params,
                    id=f"task-{self.params.task_id}",
                    task_queue=self.agent_queue,
                )
                result_task = asyncio.create_task(handle.result())
                await asyncio.wait_for(self._commit_started.wait(), timeout=5)
                stop_tool_worker.set()
                await asyncio.wait_for(tool_worker_task, timeout=5)
                self._release_commit.set()
                for _attempt in range(300):
                    history = await handle.fetch_history()
                    if any(
                        event.activity_task_scheduled_event_attributes.activity_type.name
                        == ACTIVITY_CLEANUP_RUN_WORKSPACE
                        for event in history.events
                    ):
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("cleanup activity was not scheduled")
                await environment.sleep(timedelta(seconds=31))
                return await asyncio.wait_for(result_task, timeout=5)
        finally:
            stop_tool_worker.set()
            self._release_commit.set()
            if tool_worker_task is not None and not tool_worker_task.done():
                tool_worker_task.cancel()
                await asyncio.gather(tool_worker_task, return_exceptions=True)
            await environment.shutdown()


@pytest.fixture
def two_queue_world() -> TwoQueueWorld:
    return TwoQueueWorld()


async def test_new_history_routes_each_boundary_to_its_owner(
    two_queue_world: TwoQueueWorld,
) -> None:
    result = await two_queue_world.run_one_step()

    assert result.status == "completed"
    assert two_queue_world.activity_calls == [
        (ACTIVITY_RESOLVE_SNAPSHOT, two_queue_world.agent_queue),
        (ACTIVITY_RESOLVE_ADVERTISED_TOOLS, TOOL_TASK_QUEUE),
        (ACTIVITY_REASON_AGENT_STEP, two_queue_world.agent_queue),
        (ACTIVITY_EXECUTE_BOUND_TOOL, TOOL_TASK_QUEUE),
        (ACTIVITY_COMMIT_AGENT_STEP, two_queue_world.agent_queue),
        (ACTIVITY_CLEANUP_RUN_WORKSPACE, TOOL_TASK_QUEUE),
        (ACTIVITY_FINALIZE_RUN_PROJECTION, two_queue_world.agent_queue),
    ]


@pytest.mark.parametrize(
    ("scenario", "last_boundary_activity"),
    [
        ("zero_calls", None),
        ("approval", ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL),
        ("blocking_delegation", ACTIVITY_EXECUTE_BOUND_TOOL),
        ("execution_unknown", ACTIVITY_EXECUTE_BOUND_TOOL),
        ("cancellation", ACTIVITY_CLEANUP_RUN_WORKSPACE),
        ("cleanup_failure", ACTIVITY_CLEANUP_RUN_WORKSPACE),
    ],
)
async def test_stop_scenarios_never_schedule_a_later_ordinal(
    two_queue_world: TwoQueueWorld,
    scenario: str,
    last_boundary_activity: str | None,
) -> None:
    result = await two_queue_world.run_scenario(scenario)

    assert two_queue_world.executed_ordinals == two_queue_world.expected_prefix(scenario)
    if last_boundary_activity == ACTIVITY_CLEANUP_RUN_WORKSPACE:
        assert two_queue_world.effect_activity_names[-1] == last_boundary_activity
    elif last_boundary_activity is not None:
        step_effects = [
            name
            for name in two_queue_world.effect_activity_names
            if name != ACTIVITY_CLEANUP_RUN_WORKSPACE
        ]
        assert step_effects[-1] == last_boundary_activity
    assert all(queue == TOOL_TASK_QUEUE for _name, queue in two_queue_world.effect_calls)
    assert two_queue_world.activity_calls[-2:] == [
        (ACTIVITY_CLEANUP_RUN_WORKSPACE, TOOL_TASK_QUEUE),
        (ACTIVITY_FINALIZE_RUN_PROJECTION, two_queue_world.agent_queue),
    ]
    if scenario == "cancellation":
        assert result.status == "cancelled"
        assert len(two_queue_world.commit_calls) == 1
        assert two_queue_world.commit_calls[0].cancelled_after_tool_call_id == _TOOL_CALL_ID
    elif scenario == "execution_unknown":
        assert result.status == "failed"
    else:
        assert result.status == "completed"


async def test_final_projection_is_bounded_when_cleanup_queue_has_no_poller(
    two_queue_world: TwoQueueWorld,
) -> None:
    result = await two_queue_world.run_without_cleanup_poller()

    assert result.status == "completed"
    assert two_queue_world.finalize_calls == [
        FinalizeInput(
            workspace_id=two_queue_world.params.workspace_id,
            task_id=two_queue_world.params.task_id,
            run_id=two_queue_world.run_id,
            status="completed",
            steps_used=1,
        )
    ]
    assert (ACTIVITY_CLEANUP_RUN_WORKSPACE, TOOL_TASK_QUEUE) not in two_queue_world.activity_calls
    assert two_queue_world.activity_calls[-1] == (
        ACTIVITY_FINALIZE_RUN_PROJECTION,
        two_queue_world.agent_queue,
    )


async def test_approval_resolution_crosses_tool_then_agent_boundaries(
    two_queue_world: TwoQueueWorld,
) -> None:
    await two_queue_world.run_scenario("approval")

    resolve_index = two_queue_world.activity_calls.index(
        (ACTIVITY_RESOLVE_BOUND_TOOL_APPROVAL, TOOL_TASK_QUEUE)
    )
    assert two_queue_world.activity_calls[resolve_index + 1] == (
        ACTIVITY_COMMIT_APPROVAL_PROJECTION,
        two_queue_world.agent_queue,
    )
    assert two_queue_world.approval_projection_calls == [
        CommitApprovalProjectionInput(
            workspace_id=two_queue_world.params.workspace_id,
            task_id=two_queue_world.params.task_id,
            run_id=two_queue_world.run_id,
            agent_id=two_queue_world.params.agent_id,
            approval_id=_APPROVAL_ID,
            tool_call_id=_TOOL_CALL_ID,
        )
    ]
