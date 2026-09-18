"""Durable scheduler; polling reads are authoritative and never create duplicate tasks."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.schedules.shared import ScheduleClaim, ScheduleFinish, ScheduleTick


@workflow.defn(name="AgentScheduleWorkflow")
class AgentScheduleWorkflow:
    @workflow.run
    async def run(self, params: ScheduleTick) -> None:
        for _ in range(500):
            claim = await workflow.execute_activity(
                "claim_schedule_occurrence",
                params,
                result_type=ScheduleClaim,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if claim.deleted:
                return
            if claim.task_id:
                try:
                    result = await workflow.execute_child_workflow(
                        "AgentTaskWorkflow",
                        AgentTaskInput(
                            params.workspace_id, claim.task_id, claim.agent_id, claim.brief
                        ),
                        id=f"task-{claim.task_id}",
                        result_type=AgentTaskResult,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                except (ChildWorkflowError, WorkflowAlreadyStartedError):
                    # A previous scheduler execution may already own this task.
                    # The next claim reads its durable terminal state; it must
                    # never start an alternate task or infer success from error.
                    await workflow.sleep(timedelta(seconds=15))
                    continue
                await workflow.execute_activity(
                    "finish_schedule_occurrence",
                    ScheduleFinish(params.workspace_id, claim.occurrence_id, result.status),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            else:
                await workflow.sleep(timedelta(seconds=max(1, min(60, claim.wait_seconds))))
        workflow.continue_as_new(params)
