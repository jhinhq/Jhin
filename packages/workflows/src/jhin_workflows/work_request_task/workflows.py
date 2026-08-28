"""WorkRequestTaskWorkflow: run one accepted work request's task durably.

Mirrors ``DelegatedTaskWorkflow`` without the lineage semantics: the created
task runs as an ordinary nested ``AgentTaskWorkflow`` under ``task-<id>`` (so
pause/resume/cancel/approval signals work), then a retrying activity marks
the request completed/failed and posts the standardized ``result`` message
on the requester's task.

Nothing here knows whether anyone is waiting. The requester parks on this
workflow's completion for a bounded while (``AgentTaskWorkflow.
_await_work_request_answer``) precisely because the ``result`` message is
committed before it returns; a requester that has given up, or was never
waiting, changes nothing about what this workflow does.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

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

# A colleague's answer is something a person is waiting on, so the request
# is time-boxed: an accepted request that never finishes (a task parked on
# an approval nobody decides, a queue slot that never frees, a wedged run)
# must still reach a terminal state with a readable reason instead of
# holding one of the target's ``max_active_request_tasks_per_agent`` slots
# forever. The window is generous — long enough for a legitimate human
# approval on the target's own work — because the point is a ceiling, not a
# deadline. On expiry the child is terminated and the request is finalized
# as failed.
_MAX_CHILD_EXECUTION = timedelta(hours=6)


def _timed_out(error: BaseException) -> bool:
    """Whether a child failure was the execution timeout (bounded walk)."""
    current: BaseException | None = error
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, TemporalTimeoutError):
            return True
        current = current.__cause__
    return False


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
                execution_timeout=_MAX_CHILD_EXECUTION,
            )
            run_status = child.status
        except Exception as error:
            # AgentTaskWorkflow persists its own failures; reaching here means
            # the child died abnormally or ran past the time box. The request
            # still resolves — that is the whole point of finalizing below.
            run_status = "timed_out" if _timed_out(error) else "failed"

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
