"""The story this whole change exists for, end to end: the tool worker is
redeployed in the middle of a turn and the run survives it.

Nothing tested that. Every piece was tested — the drain refuses, the gateway
re-dispatches, the sweep closes orphans — and the sentence a person actually
read ("tool call ... execution outcome is unknown; manual reconciliation is
required") comes out of none of those pieces on its own. It comes out of the
whole path, and the whole path is what this file runs: the real
``AgentTaskWorkflow``, on a real Temporal, over both task queues, with the
tool queue served by a worker holding the real :class:`WorkerDrain` — stopped
mid-tool-call and replaced, the way ``docker compose up -d`` does it.

The arithmetic is worth stating, because it is what the reproduction was
about. One replica, and a step budget of three attempts at 2s and 4s is six
seconds of patience against a restart that takes longer than that. The
refusal a draining worker returns is *instant*, so a turn arriving at the
wrong moment could spend every attempt inside one drain window and fail. Two
changes fix it together, and neither would alone: the refusal now carries its
own ``next_retry_delay``, longer than the drain, so at most one attempt can
land on a leaving worker — and once the process is gone nothing polls this
queue, so the activity task waits for a live worker rather than failing
against a dead one. The budget is then spent on real flakiness, which is what
a budget is for.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_tool_worker.drain import DRAINING_ERROR_TYPE, WorkerDrain
from jhin_workflows import AGENT_TASK_QUEUE, TOOL_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    ACTIVITY_COMMIT_AGENT_STEP,
    ACTIVITY_EXECUTE_BOUND_TOOL,
    ACTIVITY_FINALIZE_RUN_PROJECTION,
    ACTIVITY_REASON_AGENT_STEP,
    ACTIVITY_RESOLVE_ADVERTISED_TOOLS,
    ACTIVITY_RESOLVE_SNAPSHOT,
    AdvertisedTool,
    BoundToolResult,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
    CommitAgentStepInput,
    ExecuteBoundToolInput,
    FinalizeInput,
    ReasonAgentStepInput,
    ReasonAgentStepResult,
    ResolveAdvertisedToolsInput,
    SnapshotResult,
    StepResult,
)

pytestmark = pytest.mark.anyio

_TOOL_CALL_ID = "018f4d52-8b93-7d41-8ac7-7f190f093333"


class _AgentSide:
    """The activities on the agent worker. That process is not restarted."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.reason_calls = 0
        self.commits: list[CommitAgentStepInput] = []
        self.finalized: list[FinalizeInput] = []

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot(self, _params: AgentTaskInput) -> SnapshotResult:
        return SnapshotResult(
            run_id=self.run_id,
            snapshot_json="{}",
            snapshot_hash="snapshot-hash",
            max_steps=2,
        )

    @activity.defn(name=ACTIVITY_REASON_AGENT_STEP)
    async def reason_agent_step(self, _params: ReasonAgentStepInput) -> ReasonAgentStepResult:
        self.reason_calls += 1
        return ReasonAgentStepResult(call_count=1 if self.reason_calls == 1 else 0)

    @activity.defn(name=ACTIVITY_COMMIT_AGENT_STEP)
    async def commit_agent_step(self, params: CommitAgentStepInput) -> StepResult:
        self.commits.append(params)
        return StepResult(done=True)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN_PROJECTION)
    async def finalize_run_projection(self, params: FinalizeInput) -> None:
        self.finalized.append(params)


class _ToolSide:
    """One tool worker process: its drain, and what it served.

    The activities are wrapped in the drain exactly as the real ones are, so
    a refusal here is the product's refusal — same error type, same retry
    delay, same "nothing ran". The one thing the double adds is *counting*
    the refusals, which the product has no reason to do and a test has every
    reason to: "it was refused and the run still completed" is the claim.
    """

    def __init__(self, name: str, ledger: list[str], *, block: bool = False) -> None:
        self.name = name
        self.drain = WorkerDrain()
        self.ledger = ledger
        self.executed = 0
        self.refusals = 0
        self._block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        #: Lift the drain once this many calls have been refused — the
        #: replacement process being up by the time the retry lands.
        self.lift_after: int | None = None
        #: SIGTERM lands between the two tool-queue calls of a step, so the
        #: refusal falls on the one that would have dispatched an effect.
        self.drain_after_advertise = False

    @activity.defn(name=ACTIVITY_RESOLVE_ADVERTISED_TOOLS)
    async def resolve_advertised_tools(
        self, _params: ResolveAdvertisedToolsInput
    ) -> list[AdvertisedTool]:
        with self._refusals_counted("advertise"), self.drain.hold():
            self.ledger.append(f"{self.name}:advertise")
            if self.drain_after_advertise:
                self.drain.begin()
            return [
                AdvertisedTool(
                    name="cli.file.list",
                    description="List files in the sandbox checkout",
                    parameters={"type": "object"},
                )
            ]

    @activity.defn(name=ACTIVITY_EXECUTE_BOUND_TOOL)
    async def execute_bound_tool(self, _params: ExecuteBoundToolInput) -> BoundToolResult:
        with self._refusals_counted("execute"), self.drain.hold():
            self.ledger.append(f"{self.name}:execute")
            if self._block and self.executed == 0:
                self.executed += 1
                self.started.set()
                # Stands in for a sandbox job in flight. The redeploy
                # arrives here, and the worker's shutdown cancels this
                # activity — which is what cuts the real one off.
                await self.release.wait()
                raise AssertionError("this call should have been cut off")
            self.executed += 1
            return BoundToolResult(tool_call_id=_TOOL_CALL_ID, status="executed")

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace(
        self, _params: CleanupRunWorkspaceInput
    ) -> CleanupRunWorkspaceResult:
        with self._refusals_counted("cleanup"), self.drain.hold():
            self.ledger.append(f"{self.name}:cleanup")
            return CleanupRunWorkspaceResult(deleted=True)

    def _refusals_counted(self, kind: str) -> Any:
        side = self

        class _Counter:
            def __enter__(self) -> None:
                return None

            def __exit__(self, _type: Any, error: Any, _traceback: Any) -> bool:
                if isinstance(error, ApplicationError) and error.type == DRAINING_ERROR_TYPE:
                    side.refusals += 1
                    side.ledger.append(f"{side.name}:{kind}:refused")
                    if side.lift_after is not None and side.refusals >= side.lift_after:
                        # The replacement process, arriving.
                        side.drain = WorkerDrain()
                return False

        return _Counter()

    def activities(self) -> list[Any]:
        return [
            self.resolve_advertised_tools,
            self.execute_bound_tool,
            self.cleanup_run_workspace,
        ]


def _params() -> AgentTaskInput:
    return AgentTaskInput(
        workspace_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )


async def _wait_for(
    event: asyncio.Event, result: asyncio.Task[Any], *, seconds: float = 30
) -> None:
    waiter = asyncio.create_task(event.wait())
    done, _pending = await asyncio.wait(
        {waiter, result}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
    )
    if result in done:
        waiter.cancel()
        # Surfaces the workflow's own failure rather than a timeout about it.
        result.result()
        raise AssertionError("the run finished before the redeploy could happen")
    assert waiter in done, "the tool call never started"


async def test_a_redeploy_in_the_middle_of_a_tool_call_does_not_end_the_run() -> None:
    """The operator's incident, replayed against the fix.

    A turn is inside its tool call when the tool worker is replaced: the
    process stops with the call still running, and a new one comes up. The
    run used to die here with "manual reconciliation is required". It now
    finishes, having run the tool call on the worker that was alive to run it.
    """
    ledger: list[str] = []
    params = _params()
    agent_side = _AgentSide(run_id=str(uuid.uuid4()))
    leaving = _ToolSide("leaving", ledger, block=True)
    arriving = _ToolSide("arriving", ledger)

    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(
            environment.client,
            task_queue=AGENT_TASK_QUEUE,
            workflows=[AgentTaskWorkflow],
            activities=[
                agent_side.resolve_snapshot,
                agent_side.reason_agent_step,
                agent_side.commit_agent_step,
                agent_side.finalize_run_projection,
            ],
        ):
            stop_leaving = asyncio.Event()
            leaving_gone = asyncio.Event()

            async def serve_leaving() -> None:
                async with Worker(
                    environment.client,
                    task_queue=TOOL_TASK_QUEUE,
                    activities=leaving.activities(),
                ):
                    await stop_leaving.wait()
                    # SIGTERM: the door closes before the process goes.
                    leaving.drain.begin()
                leaving_gone.set()

            leaving_task = asyncio.create_task(serve_leaving())
            handle = await environment.client.start_workflow(
                AgentTaskWorkflow.run,
                params,
                id=f"task-{params.task_id}",
                task_queue=AGENT_TASK_QUEUE,
            )
            result_task = asyncio.create_task(handle.result())

            await _wait_for(leaving.started, result_task)
            stop_leaving.set()
            await asyncio.wait_for(leaving_gone.wait(), timeout=60)
            await asyncio.wait_for(leaving_task, timeout=60)

            # The replacement comes up, and the turn carries on.
            async with Worker(
                environment.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=arriving.activities(),
            ):
                result = await asyncio.wait_for(result_task, timeout=120)
    finally:
        leaving.release.set()
        await environment.shutdown()

    assert result.status == "completed"
    # The call ran on the new worker, once, and the step it belonged to was
    # committed rather than abandoned.
    assert arriving.executed == 1
    assert ledger.count("arriving:execute") == 1
    assert len(agent_side.commits) == 1
    assert agent_side.finalized[-1].status == "completed"


@pytest.mark.parametrize("refusals", [1, 3])
@pytest.mark.parametrize("refused_call", ["advertise", "execute"])
async def test_a_call_that_lands_on_a_draining_worker_is_refused_then_served(
    refusals: int,
    refused_call: str,
) -> None:
    """The other half of the same redeploy: the call arrives *while* the old
    worker is draining. It is refused before anything is claimed — so there
    is nothing to reconcile — and a later attempt is served.

    One refusal is the ordinary case, and it is the one the refusal's own
    retry delay is sized to produce: longer than the drain, so a second
    attempt cannot land on the same leaving worker. Three is the case that
    justifies the wider budget on this queue — a rolling redeploy is several
    processes leaving, and a step budget of three attempts total would have
    ended the run on the last of them with "an activity failed", for a deploy
    that went exactly to plan.
    """
    ledger: list[str] = []
    params = _params()
    agent_side = _AgentSide(run_id=str(uuid.uuid4()))
    worker_side = _ToolSide("restarting", ledger)
    if refused_call == "advertise":
        worker_side.drain.begin()
    else:
        # SIGTERM between the two tool-queue calls of the step, so the
        # refusal falls on the call that would have dispatched the effect.
        worker_side.drain_after_advertise = True
    worker_side.lift_after = refusals

    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        async with (
            Worker(
                environment.client,
                task_queue=AGENT_TASK_QUEUE,
                workflows=[AgentTaskWorkflow],
                activities=[
                    agent_side.resolve_snapshot,
                    agent_side.reason_agent_step,
                    agent_side.commit_agent_step,
                    agent_side.finalize_run_projection,
                ],
            ),
            Worker(
                environment.client,
                task_queue=TOOL_TASK_QUEUE,
                activities=worker_side.activities(),
            ),
        ):
            handle = await environment.client.start_workflow(
                AgentTaskWorkflow.run,
                params,
                id=f"task-{params.task_id}",
                task_queue=AGENT_TASK_QUEUE,
            )
            result = await asyncio.wait_for(handle.result(), timeout=120)
    finally:
        await environment.shutdown()

    assert result.status == "completed"
    assert worker_side.refusals == refusals
    assert ledger.count(f"restarting:{refused_call}:refused") == refusals
    assert worker_side.executed == 1


def test_the_draining_refusal_is_retryable_and_says_nothing_started() -> None:
    """What Temporal is handed, asserted here rather than inferred from a
    green run: a *retryable* failure, or the workflow would stop on the first
    refusal instead of trying the worker that replaced it."""
    drain = WorkerDrain()
    drain.begin()

    with pytest.raises(ApplicationError) as raised, drain.hold():
        pytest.fail("a draining worker must not run the body")

    assert raised.value.type == DRAINING_ERROR_TYPE
    assert raised.value.non_retryable is False
    assert raised.value.next_retry_delay is not None
