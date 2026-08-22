"""WorkRequestTaskWorkflow: run one accepted work request's task durably.

Mirrors ``DelegatedTaskWorkflow`` without the lineage semantics: the created
task runs as an ordinary nested ``AgentTaskWorkflow`` under ``task-<id>`` (so
pause/resume/cancel/approval signals work), then a retrying activity marks
the request completed/failed and posts the standardized ``result`` message
on the requester's task. Nothing here parks the requester: work requests are
always non-blocking.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.work_request_task.shared import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    FinalizeWorkRequestInput,
    WorkRequestTaskInput,
    WorkRequestTaskResult,
)

_FINALIZE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)


@workflow.defn(name="WorkRequestTaskWorkflow")
class WorkRequestTaskWorkflow:
    @workflow.run
    async def run(self, params: WorkRequestTaskInput) -> WorkRequestTaskResult:
        try:
            child: AgentTaskResult = await workflow.execute_child_workflow(
                "AgentTaskWorkflow",
                AgentTaskInput(
                    workspace_id=params.workspace_id,
                    task_id=params.task_id,
                    agent_id=params.agent_id,
                ),
                id=f"task-{params.task_id}",
                result_type=AgentTaskResult,
            )
            run_status = child.status
        except Exception:
            # AgentTaskWorkflow persists its own failures; reaching here means
            # the child died abnormally. The request still resolves.
            run_status = "failed"

        request_status: str = await workflow.execute_activity(
            ACTIVITY_FINALIZE_WORK_REQUEST,
            FinalizeWorkRequestInput(
                workspace_id=params.workspace_id,
                work_request_id=params.work_request_id,
                task_id=params.task_id,
                run_status=run_status,
            ),
            result_type=str,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_FINALIZE_RETRY,
        )
        return WorkRequestTaskResult(
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status=run_status,
            request_status=request_status,
        )
