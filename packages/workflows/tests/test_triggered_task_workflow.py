"""TriggeredTaskWorkflow logic against stub activities and a stub child.

Uses Temporal's time-skipping test environment; the real activities and
AgentTaskWorkflow live on the agent worker and are exercised by integration
tests. Here we verify the orchestration decisions: duplicate-task short
circuit, child invocation under the ``task-<id>`` id, optional sync-back,
and sync failure tolerance.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.triggered_task import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
    TriggeredTaskInput,
    TriggeredTaskWorkflow,
)


@workflow.defn(name="AgentTaskWorkflow")
class StubAgentTaskWorkflow:
    """Stands in for the real child; records the workflow id it ran under."""

    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        return AgentTaskResult(run_id=f"run-for-{params.task_id}", status="completed", steps_used=3)


def make_input(**overrides: Any) -> TriggeredTaskInput:
    values: dict[str, Any] = {
        "workspace_id": str(uuid.uuid4()),
        "trigger_id": str(uuid.uuid4()),
        "trigger_name": "Pick up new engineering tickets",
        "invocation_id": str(uuid.uuid4()),
        "connection_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "event_type": "connector.linear.issue.updated",
        "external_source": "linear",
        "external_id": "ENG-142",
        "title": "Fix the failing test",
        "description": "The test fails; fix it.",
        "external_url": "https://linear.example/issue/ENG-142",
        "agent_id": str(uuid.uuid4()),
    }
    values.update(overrides)
    return TriggeredTaskInput(**values)


class Stubs:
    """Configurable activity doubles capturing what the workflow sent."""

    def __init__(self, *, created: bool = True, sync_fails: bool = False) -> None:
        self.created = created
        self.sync_fails = sync_fails
        self.task_id = str(uuid.uuid4())
        self.prepare_calls: list[TriggeredTaskInput] = []
        self.sync_calls: list[SyncExternalInput] = []

    @activity.defn(name=ACTIVITY_PREPARE_TRIGGERED_TASK)
    async def prepare(self, params: TriggeredTaskInput) -> PreparedTask:
        self.prepare_calls.append(params)
        return PreparedTask(task_id=self.task_id, created=self.created)

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL)
    async def sync(self, params: SyncExternalInput) -> SyncExternalResult:
        self.sync_calls.append(params)
        if self.sync_fails:
            raise ApplicationError("provider unreachable", type="sync_external_failed")
        return SyncExternalResult(synced=True, detail="https://linear.example/comment/1")


async def run_workflow(stubs: Stubs, params: TriggeredTaskInput) -> Any:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[TriggeredTaskWorkflow, StubAgentTaskWorkflow],
            activities=[stubs.prepare, stubs.sync],
        ):
            return await env.client.execute_workflow(
                TriggeredTaskWorkflow.run,
                params,
                id=f"triggered-task-{uuid.uuid4()}",
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


async def test_creates_task_and_runs_child_to_completion() -> None:
    stubs = Stubs()
    result = await run_workflow(stubs, make_input())
    assert result.task_id == stubs.task_id
    assert result.run_status == "completed"
    assert result.created_task is True
    assert result.synced_external is False  # comment_back not enabled
    assert len(stubs.prepare_calls) == 1
    assert stubs.sync_calls == []


async def test_duplicate_active_task_skips_the_run() -> None:
    stubs = Stubs(created=False)
    result = await run_workflow(stubs, make_input(comment_back=True))
    assert result.run_status == "skipped_duplicate_task"
    assert result.created_task is False
    assert stubs.sync_calls == []  # no run happened, nothing to report


async def test_comment_back_syncs_after_the_run() -> None:
    stubs = Stubs()
    result = await run_workflow(stubs, make_input(comment_back=True))
    assert result.synced_external is True
    assert len(stubs.sync_calls) == 1
    sync = stubs.sync_calls[0]
    assert sync.external_id == "ENG-142"
    assert sync.run_status == "completed"
    assert sync.run_id == f"run-for-{stubs.task_id}"


async def test_sync_failure_does_not_fail_the_workflow() -> None:
    stubs = Stubs(sync_fails=True)
    result = await run_workflow(stubs, make_input(comment_back=True))
    assert result.run_status == "completed"
    assert result.synced_external is False
    assert len(stubs.sync_calls) == 3  # retry policy exhausted (3 attempts)
