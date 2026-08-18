"""DelegatedTaskWorkflow orchestration (plan 8.3) with stub child + activity."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.delegated_task import (
    ACTIVITY_SUMMARIZE_DELEGATION,
    DelegatedTaskInput,
    DelegatedTaskWorkflow,
    DelegationSummary,
    SummarizeDelegationInput,
)


@workflow.defn(name="AgentTaskWorkflow")
class StubAgentTaskWorkflow:
    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        # No uuid4() here: workflow code runs in Temporal's deterministic
        # sandbox, which forbids os.urandom-backed randomness.
        status = "failed" if params.task_id.startswith("f") else "completed"
        return AgentTaskResult(run_id=f"run-{params.task_id}", status=status, steps_used=2)


class Stubs:
    def __init__(self) -> None:
        self.summarize_calls: list[SummarizeDelegationInput] = []

    @activity.defn(name=ACTIVITY_SUMMARIZE_DELEGATION)
    async def summarize(self, params: SummarizeDelegationInput) -> DelegationSummary:
        self.summarize_calls.append(params)
        return DelegationSummary(
            task_id=params.child_task_id,
            status="pass" if params.kind == "review_request" else params.run_status,
            summary="QA passed on the PR branch.",
            verdict="pass" if params.kind == "review_request" else "",
            reported=True,
        )


def make_input(**overrides: Any) -> DelegatedTaskInput:
    values: dict[str, Any] = {
        "workspace_id": str(uuid.uuid4()),
        "parent_task_id": str(uuid.uuid4()),
        "child_task_id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "delegating_agent_id": str(uuid.uuid4()),
        "parent_run_id": str(uuid.uuid4()),
        "kind": "review_request",
        "blocking": True,
    }
    values.update(overrides)
    return DelegatedTaskInput(**values)


async def run_workflow(stubs: Stubs, params: DelegatedTaskInput) -> Any:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[DelegatedTaskWorkflow, StubAgentTaskWorkflow],
            activities=[stubs.summarize],
        ):
            return await env.client.execute_workflow(
                DelegatedTaskWorkflow.run,
                params,
                id=f"delegated-{params.child_task_id}",
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


async def test_runs_child_under_task_id_convention_and_summarizes() -> None:
    stubs = Stubs()
    # Keep the success case deterministic: the stub deliberately treats child
    # task ids beginning with ``f`` as failures for the test below.
    params = make_input(child_task_id="10000000-0000-4000-8000-000000000001")
    result = await run_workflow(stubs, params)
    assert result.child_task_id == params.child_task_id
    assert result.run_status == "completed"
    assert result.summary.verdict == "pass"
    assert len(stubs.summarize_calls) == 1
    call = stubs.summarize_calls[0]
    # Everything the summarizer needs flows through, including the parent
    # linkage and the blocking flag for transcript de-duplication.
    assert call.parent_task_id == params.parent_task_id
    assert call.child_task_id == params.child_task_id
    assert call.delegating_agent_id == params.delegating_agent_id
    assert call.kind == "review_request"
    assert call.run_status == "completed"
    assert call.blocking is True


async def test_failed_child_still_produces_a_summary() -> None:
    stubs = Stubs()
    # The stub child fails for task ids starting with "f".
    params = make_input(child_task_id=f"f-{uuid.uuid4()}", kind="delegation")
    result = await run_workflow(stubs, params)
    assert result.run_status == "failed"
    assert stubs.summarize_calls[0].run_status == "failed"
