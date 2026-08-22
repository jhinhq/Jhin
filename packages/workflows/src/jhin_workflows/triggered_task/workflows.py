"""TriggeredTaskWorkflow (plan 8.1): trigger match → task → agent run → sync.

Started by the event worker's TriggerMatcher with a deterministic workflow id
derived from the trigger idempotency key, so Temporal's duplicate-start
policy is the second dedupe defense after the invocation table (plan 9.4).

Steps: prepare (create or dedupe-load the externally-linked task) → child
AgentTaskWorkflow (the existing Phase 3 spine, reused unchanged) → optional
external sync (comment the outcome back on the source entity). The child
runs under workflow id ``task-<task_id>`` — the exact id the API derives —
so pause/resume/cancel/approval signals work identically on triggered tasks.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.tool_compat.shared import SyncExternalToolInput
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    ACTIVITY_SYNC_EXTERNAL_TOOL,
    PHASE10_TRIGGER_SYNC_PATCH,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
    TriggeredTaskInput,
    TriggeredTaskResult,
)

# Retryable infrastructure hiccups (db down) retry with backoff; activities
# raise non-retryable ApplicationErrors for semantic dead ends (plan 8.6).
_PREPARE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)
_SYNC_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn(name="TriggeredTaskWorkflow")
class TriggeredTaskWorkflow:
    @workflow.run
    async def run(self, params: TriggeredTaskInput) -> TriggeredTaskResult:
        prepared: PreparedTask = await workflow.execute_activity(
            ACTIVITY_PREPARE_TRIGGERED_TASK,
            params,
            result_type=PreparedTask,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_PREPARE_RETRY,
        )

        if not prepared.created:
            # An active task already covers this external entity (plan 26.8);
            # starting another run would duplicate work.
            return TriggeredTaskResult(
                task_id=prepared.task_id,
                run_status="skipped_duplicate_task",
                created_task=False,
            )

        child: AgentTaskResult = await workflow.execute_child_workflow(
            "AgentTaskWorkflow",
            AgentTaskInput(
                workspace_id=params.workspace_id,
                task_id=prepared.task_id,
                agent_id=params.agent_id,
                instruction=params.description,
            ),
            id=f"task-{prepared.task_id}",
            result_type=AgentTaskResult,
        )

        synced = False
        if params.comment_back and params.connection_id and child.run_id is not None:
            # Best-effort by design: the task outcome stands even when the
            # provider is unreachable; failures land in the run timeline.
            try:
                if workflow.patched(PHASE10_TRIGGER_SYNC_PATCH):
                    outcome: SyncExternalResult = await workflow.execute_activity(
                        ACTIVITY_SYNC_EXTERNAL_TOOL,
                        SyncExternalToolInput(
                            workspace_id=params.workspace_id,
                            task_id=prepared.task_id,
                            run_id=child.run_id,
                        ),
                        result_type=SyncExternalResult,
                        task_queue=TOOL_TASK_QUEUE,
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=_SYNC_RETRY,
                    )
                else:
                    outcome = await workflow.execute_activity(
                        ACTIVITY_SYNC_EXTERNAL,
                        SyncExternalInput(
                            workspace_id=params.workspace_id,
                            connection_id=params.connection_id,
                            external_source=params.external_source,
                            external_id=params.external_id,
                            task_id=prepared.task_id,
                            run_id=child.run_id,
                            agent_id=params.agent_id,
                            run_status=child.status,
                            trigger_name=params.trigger_name,
                        ),
                        result_type=SyncExternalResult,
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=_SYNC_RETRY,
                    )
                synced = outcome.synced
            except Exception:
                synced = False

        return TriggeredTaskResult(
            task_id=prepared.task_id,
            run_status=child.status,
            created_task=True,
            synced_external=synced,
        )
