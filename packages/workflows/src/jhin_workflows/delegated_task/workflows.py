"""DelegatedTaskWorkflow (plan 8.3): one delegated child task, durably.

Started as a child workflow by the delegating run's AgentTaskWorkflow (only
workflows start child workflows). It wraps the child agent run and the
summarization step so both survive worker restarts:

1. run the child task as a nested AgentTaskWorkflow under the API's
   ``task-<id>`` workflow-id convention — pause/resume/cancel/approval
   signals work on delegated tasks exactly like any other task;
2. summarize the child's outcome into the standardized plan-7.6 record and
   persist it as a structured ``result``/``review_result`` message on the
   parent task (plan 29) — this happens even when the delegating run has
   since been cancelled or abandoned the wait.

The parent AgentTaskWorkflow awaits this workflow's result for blocking
delegations; fire-and-forget delegations rely on the persisted message.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.delegated_task.shared import (
    ACTIVITY_SUMMARIZE_DELEGATION,
    DelegatedTaskInput,
    DelegatedTaskResult,
    DelegationSummary,
    SummarizeDelegationInput,
)

_SUMMARIZE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)


@workflow.defn(name="DelegatedTaskWorkflow")
class DelegatedTaskWorkflow:
    @workflow.run
    async def run(self, params: DelegatedTaskInput) -> DelegatedTaskResult:
        try:
            child: AgentTaskResult = await workflow.execute_child_workflow(
                "AgentTaskWorkflow",
                AgentTaskInput(
                    workspace_id=params.workspace_id,
                    task_id=params.child_task_id,
                    agent_id=params.agent_id,
                ),
                id=f"task-{params.child_task_id}",
                result_type=AgentTaskResult,
            )
            run_status = child.status
        except Exception:
            # AgentTaskWorkflow persists failures itself and returns a failed
            # result; reaching here means the child workflow died abnormally
            # (terminated, deserialization). The delegation still resolves.
            run_status = "failed"

        summary: DelegationSummary = await workflow.execute_activity(
            ACTIVITY_SUMMARIZE_DELEGATION,
            SummarizeDelegationInput(
                workspace_id=params.workspace_id,
                parent_task_id=params.parent_task_id,
                child_task_id=params.child_task_id,
                agent_id=params.agent_id,
                delegating_agent_id=params.delegating_agent_id,
                kind=params.kind,
                run_status=run_status,
                parent_run_id=params.parent_run_id,
                blocking=params.blocking,
            ),
            result_type=DelegationSummary,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_SUMMARIZE_RETRY,
        )
        return DelegatedTaskResult(
            child_task_id=params.child_task_id, run_status=run_status, summary=summary
        )
