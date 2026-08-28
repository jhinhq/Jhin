"""WorkRequestTaskWorkflow orchestration with a stub child + activity, and
AgentTaskWorkflow starting it for accepted requests (idempotently)."""

from __future__ import annotations

import uuid
from datetime import timedelta
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
    WORK_REQUEST_SIDE_REQUESTER,
    WORK_REQUEST_SIDE_RESPONDER,
    AdvertisedTool,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    WorkRequestStart,
)
from jhin_workflows.agent_task.workflows import _WORK_REQUEST_ANSWER_WAIT
from jhin_workflows.work_request_task import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED,
    FinalizeWorkRequestInput,
    NoteWorkRequestUnansweredInput,
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


# --- the requester waits for the answer (and never the responder) ---

_ANSWER_ACTIVITY = "test_mark_child_done"
# Long enough that a requester which did not wait would reach its next step
# first, and short enough to land inside the bounded wait.
_CHILD_WORK = timedelta(seconds=30)


@workflow.defn(name="WorkRequestTaskWorkflow")
class SlowWorkRequestTaskWorkflow:
    """A colleague who takes a while, and says so on the way out."""

    @workflow.run
    async def run(self, params: WorkRequestTaskInput) -> WorkRequestTaskResult:
        await workflow.sleep(_CHILD_WORK)
        await workflow.execute_activity(
            _ANSWER_ACTIVITY, start_to_close_timeout=timedelta(seconds=10)
        )
        return WorkRequestTaskResult(
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status="completed",
            request_status="completed",
        )


@workflow.defn(name="WorkRequestTaskWorkflow")
class HangingWorkRequestTaskWorkflow:
    """A colleague who never answers: parked on an approval nobody decides,
    queued behind a slot that never frees, or simply wedged."""

    @workflow.run
    async def run(self, params: WorkRequestTaskInput) -> WorkRequestTaskResult:
        await workflow.wait_condition(lambda: False)
        raise AssertionError("unreachable")


@workflow.defn(name="WorkRequestTaskWorkflow")
class CancellingWorkRequestTaskWorkflow:
    """Stops the requester mid-wait, the way a person clicking Stop does."""

    @workflow.run
    async def run(self, params: WorkRequestTaskInput) -> WorkRequestTaskResult:
        parent = workflow.info().parent
        assert parent is not None
        await workflow.get_external_workflow_handle(parent.workflow_id).signal("cancel")
        await workflow.wait_condition(lambda: False)
        raise AssertionError("unreachable")


class WaitingAgentStubs:
    """One work-request start on the first step, then an ordinary finish.

    ``events`` records the order the run's parts actually happened in, which
    is the whole question here: the requester must not reach its next model
    step before the colleague is done.
    """

    def __init__(self, start: WorkRequestStart, *, repeat: bool = False) -> None:
        self.start = start
        self.repeat = repeat
        self.events: list[str] = []
        self.notes: list[NoteWorkRequestUnansweredInput] = []
        self.elapsed = timedelta()

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
        self.events.append(f"step-{params.step_index}")
        started = self.repeat or params.step_index == 0
        return StepResult(
            done=params.step_index >= 1,
            work_request_starts=[self.start] if started else [],
        )

    @activity.defn(name=ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED)
    async def note(self, params: NoteWorkRequestUnansweredInput) -> str:
        self.notes.append(params)
        self.events.append("note")
        return "noted"

    @activity.defn(name=_ANSWER_ACTIVITY)
    async def answered(self) -> None:
        self.events.append("child")

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup(self, params: CleanupRunWorkspaceInput) -> CleanupRunWorkspaceResult:
        return CleanupRunWorkspaceResult(deleted=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize(self, params: FinalizeInput) -> None:
        return None


def requester_start(agent_id: str = "target") -> WorkRequestStart:
    return WorkRequestStart(
        work_request_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        agent_id=agent_id,
        side=WORK_REQUEST_SIDE_REQUESTER,
    )


async def run_requester(
    stubs: WaitingAgentStubs, child: type, *, agent_id: str = "asker"
) -> AgentTaskResult:
    """Run one requester end to end, recording how long the run took on the
    environment's skippable clock — how long the wait actually held is half
    of what these tests are about."""
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow, child],
                activities=[
                    stubs.resolve,
                    stubs.reason,
                    stubs.commit,
                    stubs.note,
                    stubs.answered,
                    stubs.finalize,
                ],
            ),
            Worker(
                env.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=[stubs.advertised, stubs.cleanup],
            ),
        ):
            handle = await env.client.start_workflow(
                AgentTaskWorkflow.run,
                AgentTaskInput(workspace_id="ws", task_id=str(uuid.uuid4()), agent_id=agent_id),
                id=f"task-{uuid.uuid4()}",
                task_queue=AGENT_TASK_QUEUE,
            )
            result: AgentTaskResult = await handle.result()
            description = await handle.describe()
            assert description.start_time is not None and description.close_time is not None
            stubs.elapsed = description.close_time - description.start_time
            return result
    finally:
        await env.shutdown()


async def test_the_requester_waits_for_the_colleagues_answer() -> None:
    """The person asked their agent to ask somebody. Sending the request and
    ending the turn left them with a promise; the requester must have the
    answer in hand before it reasons again, so it can reply with it."""
    stubs = WaitingAgentStubs(requester_start())
    result = await run_requester(stubs, SlowWorkRequestTaskWorkflow)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "child", "step-1"]
    assert stubs.notes == []
    # It really held for the colleague rather than racing past them.
    assert stubs.elapsed >= _CHILD_WORK
    assert stubs.elapsed < _WORK_REQUEST_ANSWER_WAIT


async def test_a_colleague_who_does_not_answer_in_time_is_reported_not_invented() -> None:
    """The wait is bounded, so a run can never park forever on a colleague.
    When it elapses the run carries on — with a mark on the task, so the next
    step tells the person the truth instead of promising an answer."""
    start = requester_start()
    stubs = WaitingAgentStubs(start)
    result = await run_requester(stubs, HangingWorkRequestTaskWorkflow)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "note", "step-1"]
    assert [note.work_request_id for note in stubs.notes] == [start.work_request_id]
    # Bounded: it gave up at the wait, not at the colleague's own six-hour
    # ceiling, and the colleague keeps running abandoned either way.
    assert _WORK_REQUEST_ANSWER_WAIT <= stubs.elapsed < 2 * _WORK_REQUEST_ANSWER_WAIT


async def test_the_responder_never_waits_on_the_task_it_just_accepted() -> None:
    """The same start is surfaced on both sides of the ask. A responder that
    parked on it would be waiting for its own work to finish, and the run
    would never move again — so this side does not wait at all.

    Run twice: once as it really arrives (the responder owns the task it
    accepted), and once with the ownership deliberately mismatched, which
    pins ``side`` as a refusal in its own right rather than a label the
    agent-id backstop happens to agree with.
    """
    for owner in ("me", "someone-else"):
        stubs = WaitingAgentStubs(
            WorkRequestStart(
                work_request_id=str(uuid.uuid4()),
                task_id=str(uuid.uuid4()),
                agent_id=owner,
                side=WORK_REQUEST_SIDE_RESPONDER,
            )
        )
        result = await run_requester(stubs, HangingWorkRequestTaskWorkflow, agent_id="me")
        assert result.status == "completed"
        assert stubs.events == ["step-0", "step-1"]
        assert stubs.notes == []
        assert stubs.elapsed < _WORK_REQUEST_ANSWER_WAIT


async def test_a_requester_that_owns_the_created_task_does_not_wait_either() -> None:
    """Second lock on the same door: the created task always belongs to the
    request's target, so an agent that owns it cannot be the one asking."""
    stubs = WaitingAgentStubs(requester_start(agent_id="me"))
    result = await run_requester(stubs, HangingWorkRequestTaskWorkflow, agent_id="me")
    assert result.status == "completed"
    assert stubs.events == ["step-0", "step-1"]
    assert stubs.elapsed < _WORK_REQUEST_ANSWER_WAIT


async def test_stopping_the_run_releases_the_wait() -> None:
    """A person clicking Stop must not have to wait out the colleague."""
    stubs = WaitingAgentStubs(requester_start())
    result = await run_requester(stubs, CancellingWorkRequestTaskWorkflow)
    assert result.status == "cancelled"
    # Released by the cancel and not by the timer, so the person is not made
    # to sit through the rest of the wait for a run they already stopped.
    assert stubs.elapsed < _WORK_REQUEST_ANSWER_WAIT
    # A cancelled run has nothing left to tell anyone, and never reasons again.
    assert stubs.events == ["step-0"]
    assert stubs.notes == []


async def test_a_repeated_start_cannot_attach_to_the_running_child() -> None:
    """A retried tool call reports the same accepted request again. Temporal
    has no way to await a child already running, so the second start stays
    the no-op it always was — never a second wait, and never a hang."""
    start = requester_start()
    stubs = WaitingAgentStubs(start, repeat=True)
    result = await run_requester(stubs, HangingWorkRequestTaskWorkflow)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "note", "step-1"]
    assert len(stubs.notes) == 1
