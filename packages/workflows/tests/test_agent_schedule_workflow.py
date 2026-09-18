import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.schedules.shared import ScheduleClaim, ScheduleFinish, ScheduleTick
from jhin_workflows.schedules.workflows import AgentScheduleWorkflow


@workflow.defn(name="AgentTaskWorkflow")
class ScheduledTaskStub:
    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        assert params.instruction == "Draft only; director reviews and publishes."
        return AgentTaskResult(run_id="run", status="completed", steps_used=1)


class ScheduleStubs:
    def __init__(self):
        self.finished = []
        self.claims = 0

    @activity.defn(name="claim_schedule_occurrence")
    async def claim(self, params: ScheduleTick) -> ScheduleClaim:
        self.claims += 1
        if self.finished:
            return ScheduleClaim(deleted=True)
        return ScheduleClaim(
            task_id="stable-task",
            occurrence_id="stable-occurrence",
            agent_id="writer",
            brief="Draft only; director reviews and publishes.",
        )

    @activity.defn(name="finish_schedule_occurrence")
    async def finish(self, params: ScheduleFinish) -> None:
        self.finished.append(params)


async def test_real_temporal_schedule_starts_exact_task_and_records_completion():
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        stubs = ScheduleStubs()
        queue = f"schedule-test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentScheduleWorkflow, ScheduledTaskStub],
            activities=[stubs.claim, stubs.finish],
        ):
            await env.client.execute_workflow(
                AgentScheduleWorkflow.run,
                ScheduleTick("workspace", "schedule"),
                id=f"test-{uuid.uuid4()}",
                task_queue=queue,
                execution_timeout=timedelta(minutes=2),
            )
        assert stubs.claims == 2
        assert len(stubs.finished) == 1
        assert stubs.finished[0].occurrence_id == "stable-occurrence"
        assert stubs.finished[0].status == "completed"
    finally:
        await env.shutdown()
