"""AgentTaskWorkflow delegation parking and queue admission (plan 7.5, 8.3,
30) against stub activities in Temporal's time-skipping environment.

The real activities live on the agent worker and are exercised by
integration tests; here we verify the durable orchestration: queued
admission loops until a slot frees, blocking delegations start a
DelegatedTaskWorkflow child and park until its summary arrives, and
non-blocking delegations fire and forget.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.agent_task import (
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    AgentTaskInput,
    AgentTaskWorkflow,
    DelegationRequest,
    FinalizeInput,
    RunStepInput,
    SnapshotResult,
    StepResult,
)
from jhin_workflows.delegated_task import (
    ACTIVITY_DELIVER_DELEGATION_RESULT,
    DelegatedTaskInput,
    DelegatedTaskResult,
    DelegationSummary,
    DeliverDelegationResultInput,
)

_GATEWAY_TOOL_CALL_ID = "13ae7bd1-99c4-5c2f-b73b-5ae3aa201a21"


@workflow.defn(name="DelegatedTaskWorkflow")
class StubDelegatedTaskWorkflow:
    """Stands in for the real delegation wrapper; echoes its input back
    through the summary so the parent's handling can be asserted."""

    @workflow.run
    async def run(self, params: DelegatedTaskInput) -> DelegatedTaskResult:
        return DelegatedTaskResult(
            child_task_id=params.child_task_id,
            run_status="completed",
            summary=DelegationSummary(
                task_id=params.child_task_id,
                status="pass" if params.kind == "review_request" else "completed",
                summary=f"child of {params.parent_task_id} by {params.agent_id} done",
                verdict="pass" if params.kind == "review_request" else "",
                reported=True,
            ),
        )


class Stubs:
    """Configurable activity doubles capturing what the workflow sent."""

    def __init__(
        self,
        *,
        queued_times: int = 0,
        steps: list[StepResult] | None = None,
    ) -> None:
        self.queued_times = queued_times
        self.steps = steps or [StepResult(done=True)]
        self.run_id = str(uuid.uuid4())
        self.resolve_calls = 0
        self.step_calls: list[RunStepInput] = []
        self.deliver_calls: list[DeliverDelegationResultInput] = []
        self.finalize_calls: list[FinalizeInput] = []

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve(self, params: AgentTaskInput) -> SnapshotResult:
        self.resolve_calls += 1
        if self.resolve_calls <= self.queued_times:
            return SnapshotResult(
                run_id="",
                snapshot_json="",
                snapshot_hash="",
                max_steps=0,
                queued=True,
                queue_reason="agent_concurrency",
            )
        return SnapshotResult(
            run_id=self.run_id, snapshot_json="{}", snapshot_hash="h", max_steps=10
        )

    @activity.defn(name=ACTIVITY_RUN_AGENT_STEP)
    async def step(self, params: RunStepInput) -> StepResult:
        self.step_calls.append(params)
        return self.steps[min(len(self.step_calls) - 1, len(self.steps) - 1)]

    @activity.defn(name=ACTIVITY_DELIVER_DELEGATION_RESULT)
    async def deliver(self, params: DeliverDelegationResultInput) -> None:
        self.deliver_calls.append(params)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN)
    async def finalize(self, params: FinalizeInput) -> None:
        self.finalize_calls.append(params)


def make_input() -> AgentTaskInput:
    return AgentTaskInput(
        workspace_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )


async def run_workflow(stubs: Stubs, params: AgentTaskInput) -> Any:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentTaskWorkflow, StubDelegatedTaskWorkflow],
            activities=[stubs.resolve, stubs.step, stubs.deliver, stubs.finalize],
        ):
            return await env.client.execute_workflow(
                AgentTaskWorkflow.run,
                params,
                id=f"task-{params.task_id}",
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


def delegation_step(*, blocking: bool, kind: str = "delegation") -> StepResult:
    return StepResult(
        done=False,
        delegations=[
            DelegationRequest(
                child_task_id=str(uuid.uuid4()),
                target_agent_id=str(uuid.uuid4()),
                blocking=blocking,
                kind=kind,
                provider_call_id="call-1",
                gateway_tool_call_id=_GATEWAY_TOOL_CALL_ID,
            )
        ],
    )


# --- queue admission (plan 30) ---


async def test_queued_admission_retries_until_a_slot_frees() -> None:
    stubs = Stubs(queued_times=2)
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert result.run_id == stubs.run_id
    # Two queued responses, then admission (the poll timer fires under
    # time-skipping without real waiting).
    assert stubs.resolve_calls == 3
    assert stubs.finalize_calls[-1].status == "completed"


async def test_admitted_immediately_when_slot_free() -> None:
    stubs = Stubs()
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert stubs.resolve_calls == 1


# --- delegation (plan 7.5, 8.3) ---


async def test_blocking_delegation_parks_and_delivers_summary() -> None:
    step = delegation_step(blocking=True)
    stubs = Stubs(steps=[step, StepResult(done=True)])
    params = make_input()
    result = await run_workflow(stubs, params)
    assert result.status == "completed"
    assert len(stubs.step_calls) == 2  # step 2 ran only after the child result
    assert len(stubs.deliver_calls) == 1
    delivered = stubs.deliver_calls[0]
    request = step.delegations[0]
    assert delivered.child_task_id == request.child_task_id
    assert delivered.provider_call_id == "call-1"
    assert delivered.gateway_tool_call_id == _GATEWAY_TOOL_CALL_ID
    assert delivered.task_id == params.task_id
    assert delivered.run_id == stubs.run_id
    # The stub child echoed the parent linkage through the summary.
    assert delivered.summary.task_id == request.child_task_id
    assert delivered.summary.status == "completed"
    expected = f"child of {params.task_id} by {request.target_agent_id} done"
    assert delivered.summary.summary == expected


async def test_blocking_review_request_carries_verdict() -> None:
    step = delegation_step(blocking=True, kind="review_request")
    stubs = Stubs(steps=[step, StepResult(done=True)])
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert stubs.deliver_calls[0].summary.verdict == "pass"
    assert stubs.deliver_calls[0].kind == "review_request"


async def test_non_blocking_delegation_does_not_park() -> None:
    step = delegation_step(blocking=False)
    stubs = Stubs(steps=[step, StepResult(done=True)])
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    # Fire-and-forget: no deliver activity; the summarize activity of the
    # child workflow persists the result message instead.
    assert stubs.deliver_calls == []
    assert len(stubs.step_calls) == 2
