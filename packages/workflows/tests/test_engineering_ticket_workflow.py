"""EngineeringTicketWorkflow orchestration (plan 8.4, 27): implementation,
review loop, fail→fix→retest bounding — against stub activities and stub
child workflows in the time-skipping environment.
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.delegated_task import (
    DelegatedTaskInput,
    DelegatedTaskResult,
    DelegationSummary,
)
from jhin_workflows.engineering_ticket import (
    ACTIVITY_CREATE_ENGINEERING_CHILD_TASK,
    ACTIVITY_FINALIZE_ENGINEERING_TICKET,
    ACTIVITY_RESOLVE_ENGINEERING_PLAN,
    CreatedEngineeringChildTask,
    CreateEngineeringChildTaskInput,
    EngineeringPlan,
    EngineeringPlanInput,
    EngineeringTicketInput,
    EngineeringTicketWorkflow,
    FinalizeEngineeringTicketInput,
)
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
    TriggeredTaskInput,
)

IMPLEMENTER = str(uuid.uuid4())
QA = str(uuid.uuid4())
COORDINATOR = str(uuid.uuid4())


@workflow.defn(name="AgentTaskWorkflow")
class StubAgentTaskWorkflow:
    """Direct-mode implementation run."""

    @workflow.run
    async def run(self, params: AgentTaskInput) -> AgentTaskResult:
        return AgentTaskResult(run_id=f"run-{params.task_id}", status="completed", steps_used=3)


@workflow.defn(name="DelegatedTaskWorkflow")
class StubDelegatedTaskWorkflow:
    """Scripted delegation results: review_request tasks whose child-task id
    was created with a "fail-first" script fail on the QA review of cycle 1
    and pass afterwards (the create activity encodes the cycle in the id)."""

    @workflow.run
    async def run(self, params: DelegatedTaskInput) -> DelegatedTaskResult:
        if params.kind == "review_request":
            fail = "failref-" in params.child_task_id
            return DelegatedTaskResult(
                child_task_id=params.child_task_id,
                run_status="completed",
                run_id=f"run-{params.child_task_id}",
                summary=DelegationSummary(
                    task_id=params.child_task_id,
                    status="fail" if fail else "pass",
                    summary="tests fail: 1 failed" if fail else "all tests pass",
                    verdict="fail" if fail else "pass",
                    reported=True,
                ),
            )
        return DelegatedTaskResult(
            child_task_id=params.child_task_id,
            run_status="completed",
            run_id=f"run-{params.child_task_id}",
            summary=DelegationSummary(
                task_id=params.child_task_id,
                status="completed",
                summary="implemented; PR opened",
                artifacts=[{"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}],
                reported=True,
            ),
        )


class Stubs:
    def __init__(
        self,
        *,
        coordinator_mode: bool = False,
        qa_agent_id: str = QA,
        review_fails_first: bool = False,
        review_always_fails: bool = False,
    ) -> None:
        self.coordinator_mode = coordinator_mode
        self.qa_agent_id = qa_agent_id
        self.review_fails_first = review_fails_first
        self.review_always_fails = review_always_fails
        self.task_id = str(uuid.uuid4())
        self.created_children: list[CreateEngineeringChildTaskInput] = []
        self.finalize_calls: list[FinalizeEngineeringTicketInput] = []
        self.sync_calls: list[SyncExternalInput] = []

    @activity.defn(name=ACTIVITY_PREPARE_TRIGGERED_TASK)
    async def prepare(self, params: TriggeredTaskInput) -> PreparedTask:
        return PreparedTask(task_id=self.task_id, created=True)

    @activity.defn(name=ACTIVITY_RESOLVE_ENGINEERING_PLAN)
    async def resolve_plan(self, params: EngineeringPlanInput) -> EngineeringPlan:
        return EngineeringPlan(
            implementer_agent_id=IMPLEMENTER,
            coordinator_mode=self.coordinator_mode,
            qa_agent_id=self.qa_agent_id,
            manager_agent_id="",
        )

    @activity.defn(name=ACTIVITY_CREATE_ENGINEERING_CHILD_TASK)
    async def create_child(
        self, params: CreateEngineeringChildTaskInput
    ) -> CreatedEngineeringChildTask:
        self.created_children.append(params)
        marker = ""
        if params.kind == "review_request" and (
            self.review_always_fails or (self.review_fails_first and params.cycle == 1)
        ):
            marker = f"failref-c{params.cycle}-"
        return CreatedEngineeringChildTask(child_task_id=f"{marker}{uuid.uuid4()}")

    @activity.defn(name=ACTIVITY_FINALIZE_ENGINEERING_TICKET)
    async def finalize(self, params: FinalizeEngineeringTicketInput) -> None:
        self.finalize_calls.append(params)

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL)
    async def sync(self, params: SyncExternalInput) -> SyncExternalResult:
        self.sync_calls.append(params)
        return SyncExternalResult(synced=True)


def make_input(**overrides: Any) -> EngineeringTicketInput:
    base = TriggeredTaskInput(
        workspace_id=str(uuid.uuid4()),
        trigger_id=str(uuid.uuid4()),
        trigger_name="Engineering tickets",
        invocation_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        event_id=str(uuid.uuid4()),
        event_type="connector.linear.issue.updated",
        external_source="linear",
        external_id="ENG-7",
        title="Fix the failing endpoint",
        description="The endpoint 500s; fix it.",
        external_url="",
        agent_id=COORDINATOR,
        comment_back=False,
    )
    values: dict[str, Any] = {"base": base}
    values.update(overrides)
    return EngineeringTicketInput(**values)


async def run_workflow(stubs: Stubs, params: EngineeringTicketInput) -> Any:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[EngineeringTicketWorkflow, StubAgentTaskWorkflow, StubDelegatedTaskWorkflow],
            activities=[
                stubs.prepare,
                stubs.resolve_plan,
                stubs.create_child,
                stubs.finalize,
                stubs.sync,
            ],
        ):
            return await env.client.execute_workflow(
                EngineeringTicketWorkflow.run,
                params,
                id=f"engineering-{uuid.uuid4()}",
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


async def test_direct_mode_pass_first_cycle() -> None:
    stubs = Stubs()
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert result.verdict == "pass"
    assert result.cycles_used == 1
    # One QA review child; implementation ran on the main task directly.
    kinds = [c.kind for c in stubs.created_children]
    assert kinds == ["review_request"]
    assert stubs.created_children[0].target_agent_id == QA
    assert stubs.created_children[0].delegated_by_agent_id == IMPLEMENTER
    assert stubs.finalize_calls[0].status == "completed"


async def test_coordinator_mode_delegates_implementation() -> None:
    stubs = Stubs(coordinator_mode=True)
    result = await run_workflow(stubs, make_input(implementer_agent_id=IMPLEMENTER))
    assert result.status == "completed"
    kinds = [c.kind for c in stubs.created_children]
    assert kinds == ["delegation", "review_request"]
    impl = stubs.created_children[0]
    assert impl.target_agent_id == IMPLEMENTER
    assert impl.delegated_by_agent_id == COORDINATOR  # CTO delegates (plan 27)
    review = stubs.created_children[1]
    assert review.delegated_by_agent_id == COORDINATOR
    # The PR artifact from the implementation summary flows into the review.
    assert review.artifacts == [
        {"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}
    ]


async def test_failure_fix_retest_loop() -> None:
    stubs = Stubs(review_fails_first=True)
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert result.verdict == "pass"
    assert result.cycles_used == 2
    kinds = [c.kind for c in stubs.created_children]
    # QA fail → fix child task to the implementer → QA retest → pass.
    assert kinds == ["review_request", "delegation", "review_request"]
    fix = stubs.created_children[1]
    assert fix.target_agent_id == IMPLEMENTER
    assert "tests fail: 1 failed" in fix.instructions  # failure context carried
    retest = stubs.created_children[2]
    assert retest.cycle == 2
    assert "retest cycle 2" in retest.instructions


async def test_loop_is_bounded_by_max_retest_cycles() -> None:
    stubs = Stubs(review_always_fails=True)
    result = await run_workflow(stubs, make_input(max_retest_cycles=2))
    assert result.status == "review_failed"
    assert result.verdict == "fail"
    assert result.cycles_used == 2
    kinds = [c.kind for c in stubs.created_children]
    # cycle 1: review + fix; cycle 2: review, then the bound stops the loop.
    assert kinds == ["review_request", "delegation", "review_request"]
    assert stubs.finalize_calls[0].status == "review_failed"


async def test_no_qa_agent_completes_after_implementation() -> None:
    stubs = Stubs(qa_agent_id="")
    result = await run_workflow(stubs, make_input())
    assert result.status == "completed"
    assert result.verdict == ""
    assert result.cycles_used == 0
    assert stubs.created_children == []


async def test_comment_back_syncs_with_final_status() -> None:
    stubs = Stubs()
    params = make_input()
    params.base.comment_back = True
    result = await run_workflow(stubs, params)
    assert result.synced_external is True
    assert stubs.sync_calls[0].run_status == "completed"
    assert stubs.sync_calls[0].run_id == f"run-{stubs.task_id}"
