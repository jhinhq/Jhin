"""The run holds its turn open while a person answers.

A question with nobody waiting for the answer is a box on a screen that does
nothing: the agent would have already replied, and the answer would arrive
addressed to nobody. So the run parks — bounded, released by a cancel, and
delivering an observation either way, because the alternative to "nobody
answered" is a model inventing one.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

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
    ACTIVITY_DELIVER_QUESTION_ANSWER,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    SIGNAL_QUESTION_ANSWER,
    AdvertisedTool,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    DeliverQuestionAnswerInput,
    PersonQuestionAsk,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
)
from jhin_workflows.agent_task.workflows import _PERSON_ANSWER_WAIT

QUESTION_ID = "0192aaaa-0000-7000-8000-000000000001"


def test_the_row_expires_exactly_when_the_run_stops_waiting() -> None:
    """Two sides of one promise. If the row outlived the wait, a person could
    answer a live-looking box whose run had already moved on."""
    from jhin_tools.ask_person import PERSON_ANSWER_WAIT

    assert _PERSON_ANSWER_WAIT == PERSON_ANSWER_WAIT


class AskingStubs:
    """One step that asks, then an ordinary finish.

    ``events`` records the order the run's parts actually happened in, which
    is the whole question: the run must not reason again before the answer,
    or the reply is composed without it.
    """

    def __init__(self, *, ask: PersonQuestionAsk | None = None) -> None:
        self.ask = ask or PersonQuestionAsk(
            question_id=QUESTION_ID, gateway_tool_call_id=str(uuid.uuid4())
        )
        self.events: list[str] = []
        self.delivered: list[DeliverQuestionAnswerInput] = []
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
        return StepResult(
            done=params.step_index >= 1,
            person_questions=[self.ask] if params.step_index == 0 else [],
        )

    @activity.defn(name=ACTIVITY_DELIVER_QUESTION_ANSWER)
    async def deliver(self, params: DeliverQuestionAnswerInput) -> None:
        self.delivered.append(params)
        self.events.append(f"deliver-{params.outcome}")

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup(self, params: CleanupRunWorkspaceInput) -> CleanupRunWorkspaceResult:
        return CleanupRunWorkspaceResult(deleted=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize(self, params: FinalizeInput) -> None:
        self.events.append(f"finalize-{params.status}")


@workflow.defn(name="Answerer")
class AnswererWorkflow:
    """Stands in for a person: waits a while, then signals the run."""

    @workflow.run
    async def run(self, target: str, delay_seconds: int, question_id: str) -> None:
        await workflow.sleep(timedelta(seconds=delay_seconds))
        handle = workflow.get_external_workflow_handle(target)
        await handle.signal(SIGNAL_QUESTION_ANSWER, args=[question_id])


@workflow.defn(name="Stopper")
class StopperWorkflow:
    """Stands in for a person clicking Stop while the box is still up."""

    @workflow.run
    async def run(self, target: str, delay_seconds: int) -> None:
        await workflow.sleep(timedelta(seconds=delay_seconds))
        await workflow.get_external_workflow_handle(target).signal("cancel")


async def run_asking(
    stubs: AskingStubs,
    *,
    answer_after: int | None = None,
    signal_first: bool = False,
    cancel_after: int | None = None,
) -> AgentTaskResult:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow, AnswererWorkflow, StopperWorkflow],
                activities=[
                    stubs.resolve,
                    stubs.reason,
                    stubs.commit,
                    stubs.deliver,
                    stubs.finalize,
                ],
            ),
            Worker(
                env.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=[stubs.advertised, stubs.cleanup],
            ),
        ):
            workflow_id = f"task-{uuid.uuid4()}"
            handle = await env.client.start_workflow(
                AgentTaskWorkflow.run,
                AgentTaskInput(workspace_id="ws", task_id=str(uuid.uuid4()), agent_id="ada"),
                id=workflow_id,
                task_queue=AGENT_TASK_QUEUE,
            )
            if signal_first:
                # Races the park deliberately: the answer must not be lost
                # because it arrived while the step was still committing.
                await handle.signal(SIGNAL_QUESTION_ANSWER, args=[stubs.ask.question_id])
            if answer_after is not None:
                await env.client.start_workflow(
                    AnswererWorkflow.run,
                    args=[workflow_id, answer_after, stubs.ask.question_id],
                    id=f"answer-{uuid.uuid4()}",
                    task_queue=AGENT_TASK_QUEUE,
                )
            if cancel_after is not None:
                await env.client.start_workflow(
                    StopperWorkflow.run,
                    args=[workflow_id, cancel_after],
                    id=f"stop-{uuid.uuid4()}",
                    task_queue=AGENT_TASK_QUEUE,
                )
            result: AgentTaskResult = await handle.result()
            description = await handle.describe()
            assert description.start_time is not None and description.close_time is not None
            stubs.elapsed = description.close_time - description.start_time
            return result
    finally:
        await env.shutdown()


async def test_the_run_waits_for_the_answer_before_it_reasons_again() -> None:
    stubs = AskingStubs()
    result = await run_asking(stubs, answer_after=120)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "deliver-answered", "step-1", "finalize-completed"]
    assert [d.question_id for d in stubs.delivered] == [stubs.ask.question_id]
    # It really held rather than racing past the person.
    assert stubs.elapsed >= timedelta(seconds=120)
    assert stubs.elapsed < _PERSON_ANSWER_WAIT


async def test_an_answer_that_races_the_park_still_resumes_the_run() -> None:
    """The signal can land before the workflow reaches the wait; storing it
    is what stops the run sitting out the full timeout for an answer it
    already has."""
    stubs = AskingStubs()
    result = await run_asking(stubs, signal_first=True)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "deliver-answered", "step-1", "finalize-completed"]
    assert stubs.elapsed < _PERSON_ANSWER_WAIT


async def test_nobody_answering_is_bounded_and_said_out_loud() -> None:
    """A parked run must always end. When the wait elapses the agent is told
    plainly, so the next step says so instead of inventing an answer."""
    stubs = AskingStubs()
    result = await run_asking(stubs)
    assert result.status == "completed"
    assert stubs.events == ["step-0", "deliver-timed_out", "step-1", "finalize-completed"]
    assert [d.outcome for d in stubs.delivered] == ["timed_out"]
    assert _PERSON_ANSWER_WAIT <= stubs.elapsed < 2 * _PERSON_ANSWER_WAIT


async def test_stopping_the_run_releases_the_wait_and_delivers_nothing() -> None:
    """A person clicking Stop must not have to wait out their own question,
    and a cancelled run has nobody left to tell."""
    stubs = AskingStubs()
    result = await run_asking(stubs, cancel_after=60)
    assert result.status == "cancelled"
    assert stubs.events == ["step-0", "finalize-cancelled"]
    assert stubs.delivered == []
    assert stubs.elapsed < _PERSON_ANSWER_WAIT


async def test_the_delivery_carries_the_call_it_has_to_answer() -> None:
    """The observation is stitched onto the ask's own tool call; without the
    binding it would be an orphan block the provider rejects."""
    ask = PersonQuestionAsk(
        question_id=QUESTION_ID,
        provider_call_id="toolu_abc",
        gateway_tool_call_id=str(uuid.uuid4()),
    )
    stubs = AskingStubs(ask=ask)
    await run_asking(stubs, answer_after=10)
    assert len(stubs.delivered) == 1
    delivered = stubs.delivered[0]
    assert delivered.gateway_tool_call_id == ask.gateway_tool_call_id
    assert delivered.provider_call_id == "toolu_abc"
    assert delivered.run_id == "run-1"
    assert delivered.agent_id == "ada"


@workflow.defn(name="AgentTaskWorkflow", sandboxed=False)
class PreAskAgentTaskWorkflow(AgentTaskWorkflow):
    """The workflow exactly as it was before agents could ask anybody.

    Used to *record* a history, which the current workflow then replays. The
    override emits no patch marker, no timer, and no activity — which is what
    every history recorded before this release looks like.
    """

    async def _await_person_answer(self, params, run_id, ask) -> None:  # type: ignore[no-untyped-def]
        return None

    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        return await AgentTaskWorkflow.run(self, params)


async def test_a_run_recorded_before_agents_could_ask_still_replays() -> None:
    """``workflow.patched`` is what keeps an in-flight run from suddenly
    growing a timer and an activity its history has never heard of. Without
    the guard this replay fails as non-deterministic."""
    from temporalio.worker import Replayer

    stubs = AskingStubs()
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                env.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[PreAskAgentTaskWorkflow],
                activities=[
                    stubs.resolve,
                    stubs.reason,
                    stubs.commit,
                    stubs.deliver,
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
                AgentTaskInput(workspace_id="ws", task_id=str(uuid.uuid4()), agent_id="ada"),
                id=f"task-{uuid.uuid4()}",
                task_queue=AGENT_TASK_QUEUE,
            )
            await handle.result()
            # The old shape: it asked, and carried straight on without waiting.
            assert stubs.events == ["step-0", "step-1", "finalize-completed"]
            assert stubs.delivered == []
            history = await handle.fetch_history()
    finally:
        await env.shutdown()

    await Replayer(workflows=[AgentTaskWorkflow]).replay_workflow(history)
