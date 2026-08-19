"""EngineeringTicketWorkflow (plan 8.4): the built-in engineering template.

issue → implement → PR → optional manager review → QA review_request →
fail→fix→retest loop (bounded) → pass → optional external sync. Merge stays
approval-gated through the existing tool gateway — this workflow never
bypasses policy (plan 52); it only routes work.

Two modes (plan 27 lifecycle):

- **direct**: the trigger targets the implementer (e.g. Senior SWE); the
  main task runs as a plain AgentTaskWorkflow, then reviews run as delegated
  child tasks.
- **coordinator**: ``implementer_agent_id`` is configured and differs from
  the trigger target (e.g. trigger targets the CTO); the CTO owns the ticket
  and implementation itself becomes a delegated child task — manager routing
  end to end.

Every hop is a real child task + DelegatedTaskWorkflow, so lineage, timeline,
structured messages, and summaries look exactly like agent-driven delegation.
The template's delegations are authorized by configuration: a human with
trigger rights selected this template and its agents in the trigger
definition (same standing-authority model as comment-back sync, plan 26.14).

This is a TEMPLATE: plain TriggeredTaskWorkflow remains the default and no
engineering assumption leaks into core models (plan 28).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import AgentTaskInput, AgentTaskResult
from jhin_workflows.delegated_task.shared import (
    DelegatedTaskInput,
    DelegatedTaskResult,
    DelegationSummary,
)
from jhin_workflows.engineering_ticket.shared import (
    ACTIVITY_CREATE_ENGINEERING_CHILD_TASK,
    ACTIVITY_FINALIZE_ENGINEERING_TICKET,
    ACTIVITY_RESOLVE_ENGINEERING_PLAN,
    PHASE10_ENGINEERING_SYNC_PATCH,
    CreatedEngineeringChildTask,
    CreateEngineeringChildTaskInput,
    EngineeringPlan,
    EngineeringPlanInput,
    EngineeringTicketInput,
    EngineeringTicketResult,
    FinalizeEngineeringTicketInput,
)
from jhin_workflows.tool_compat.shared import SyncExternalToolInput
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    ACTIVITY_SYNC_EXTERNAL_TOOL,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
)

_ACTIVITY_RETRY = RetryPolicy(
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


@workflow.defn(name="EngineeringTicketWorkflow")
class EngineeringTicketWorkflow:
    def __init__(self) -> None:
        self._phase = "starting"
        self._cycle = 0

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"phase": self._phase, "cycle": self._cycle}

    @workflow.run
    async def run(self, params: EngineeringTicketInput) -> EngineeringTicketResult:
        base = params.base
        max_cycles = max(1, min(10, params.max_retest_cycles))

        self._phase = "preparing"
        prepared: PreparedTask = await workflow.execute_activity(
            ACTIVITY_PREPARE_TRIGGERED_TASK,
            base,
            result_type=PreparedTask,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY,
        )
        if not prepared.created:
            return EngineeringTicketResult(
                task_id=prepared.task_id, status="skipped_duplicate_task", created_task=False
            )

        plan: EngineeringPlan = await workflow.execute_activity(
            ACTIVITY_RESOLVE_ENGINEERING_PLAN,
            EngineeringPlanInput(
                workspace_id=base.workspace_id,
                task_id=prepared.task_id,
                coordinator_agent_id=base.agent_id,
                implementer_agent_id=params.implementer_agent_id,
                qa_agent_id=params.qa_agent_id,
                manager_review=params.manager_review,
            ),
            result_type=EngineeringPlan,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY,
        )

        # --- implementation ---
        self._phase = "implementing"
        artifacts: list[dict[str, Any]] = []
        impl_run_id = ""
        if plan.coordinator_mode:
            impl = await self._delegate(
                base.workspace_id,
                parent_task_id=prepared.task_id,
                target_agent_id=plan.implementer_agent_id,
                delegated_by_agent_id=base.agent_id,
                kind="delegation",
                title=f"Implement: {base.title}"[:500],
                instructions=(
                    f"{base.description}\n\nImplement this ticket: make the change in "
                    "your sandbox, run the tests, open a pull request, and report the "
                    "result with organization.report_result including the PR as an "
                    "artifact."
                ),
                expected_output="An open pull request implementing the ticket",
            )
            impl_status = impl.run_status
            artifacts = list(impl.summary.artifacts)
            impl_run_id = impl.run_id
        else:
            child: AgentTaskResult = await workflow.execute_child_workflow(
                "AgentTaskWorkflow",
                AgentTaskInput(
                    workspace_id=base.workspace_id,
                    task_id=prepared.task_id,
                    agent_id=plan.implementer_agent_id,
                    instruction=base.description,
                ),
                id=f"task-{prepared.task_id}",
                result_type=AgentTaskResult,
            )
            impl_status = child.status
            impl_run_id = child.run_id or ""

        # --- review loop (plan 27: fail → fix child task → retest) ---
        verdict = ""
        cycles_used = 0
        if impl_status != "completed":
            final_status = "implementation_failed"
        else:
            final_status = "completed"
            reviewers: list[tuple[str, str]] = []
            if plan.manager_agent_id:
                reviewers.append(("manager", plan.manager_agent_id))
            if plan.qa_agent_id:
                reviewers.append(("qa", plan.qa_agent_id))
            delegated_by = base.agent_id if plan.coordinator_mode else plan.implementer_agent_id
            failure_context = ""
            while reviewers:
                self._cycle += 1
                cycles_used = self._cycle
                self._phase = "reviewing"
                failed: DelegationSummary | None = None
                for role, reviewer_id in reviewers:
                    review = await self._delegate(
                        base.workspace_id,
                        parent_task_id=prepared.task_id,
                        target_agent_id=reviewer_id,
                        delegated_by_agent_id=delegated_by,
                        kind="review_request",
                        title=(
                            f"{'Manager review' if role == 'manager' else 'QA review'}: "
                            f"{base.title}"
                        )[:500],
                        instructions=self._review_instructions(
                            base.title, self._cycle, failure_context
                        ),
                        expected_output="status='pass' or 'fail' via organization.report_result",
                        artifacts=artifacts,
                        cycle=self._cycle,
                    )
                    verdict = review.summary.verdict
                    if verdict != "pass":
                        failed = review.summary
                        break
                if failed is None:
                    break  # every reviewer passed
                if self._cycle >= max_cycles:
                    final_status = "review_failed"
                    break

                # Fail → fix: a NEW child task to the implementer carrying the
                # review_result as context (plan 27), then retest.
                self._phase = "fixing"
                fix = await self._delegate(
                    base.workspace_id,
                    parent_task_id=prepared.task_id,
                    target_agent_id=plan.implementer_agent_id,
                    delegated_by_agent_id=delegated_by,
                    kind="delegation",
                    title=f"Fix (cycle {self._cycle}): {base.title}"[:500],
                    instructions=(
                        f"The review failed. Reviewer summary:\n{failed.summary}\n\n"
                        "Fix the problems, update the pull request, and report the "
                        "result with organization.report_result."
                    ),
                    expected_output="The pull request updated with the fix",
                    artifacts=list(failed.artifacts) or artifacts,
                    cycle=self._cycle,
                )
                if fix.run_status != "completed":
                    final_status = "implementation_failed"
                    break
                if fix.summary.artifacts:
                    artifacts = list(fix.summary.artifacts)
                failure_context = failed.summary

        # --- finalize + optional sync-back ---
        self._phase = "finalizing"
        await workflow.execute_activity(
            ACTIVITY_FINALIZE_ENGINEERING_TICKET,
            FinalizeEngineeringTicketInput(
                workspace_id=base.workspace_id,
                task_id=prepared.task_id,
                status=final_status,
                verdict=verdict,
                cycles_used=cycles_used,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY,
        )

        synced = False
        if base.comment_back and base.connection_id and impl_run_id:
            try:
                if workflow.patched(PHASE10_ENGINEERING_SYNC_PATCH):
                    outcome: SyncExternalResult = await workflow.execute_activity(
                        ACTIVITY_SYNC_EXTERNAL_TOOL,
                        SyncExternalToolInput(
                            workspace_id=base.workspace_id,
                            task_id=prepared.task_id,
                            run_id=impl_run_id,
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
                            workspace_id=base.workspace_id,
                            connection_id=base.connection_id,
                            external_source=base.external_source,
                            external_id=base.external_id,
                            task_id=prepared.task_id,
                            run_id=impl_run_id,
                            agent_id=plan.implementer_agent_id,
                            run_status=final_status,
                            trigger_name=base.trigger_name,
                        ),
                        result_type=SyncExternalResult,
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=_SYNC_RETRY,
                    )
                synced = outcome.synced
            except Exception:
                synced = False

        self._phase = final_status
        return EngineeringTicketResult(
            task_id=prepared.task_id,
            status=final_status,
            verdict=verdict,
            cycles_used=cycles_used,
            created_task=True,
            synced_external=synced,
        )

    @staticmethod
    def _review_instructions(title: str, cycle: int, failure_context: str) -> str:
        text = (
            f"Review the implementation of: {title}. Check out the pull request "
            "branch in your sandbox, run the test suite, and report "
            "status='pass' or status='fail' with organization.report_result, "
            "including concrete evidence (test output) in the summary."
        )
        if cycle > 1 and failure_context:
            text += (
                f"\n\nThis is retest cycle {cycle}. The previous review failed "
                f"with:\n{failure_context}\nVerify those problems are fixed."
            )
        return text

    async def _delegate(
        self,
        workspace_id: str,
        *,
        parent_task_id: str,
        target_agent_id: str,
        delegated_by_agent_id: str,
        kind: str,
        title: str,
        instructions: str,
        expected_output: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        cycle: int = 0,
    ) -> DelegatedTaskResult:
        """One template hop: create the child task row, then run it durably
        through DelegatedTaskWorkflow (identical shape to agent-driven
        delegation, so lineage/messages/summaries are uniform)."""
        created: CreatedEngineeringChildTask = await workflow.execute_activity(
            ACTIVITY_CREATE_ENGINEERING_CHILD_TASK,
            CreateEngineeringChildTaskInput(
                workspace_id=workspace_id,
                parent_task_id=parent_task_id,
                target_agent_id=target_agent_id,
                delegated_by_agent_id=delegated_by_agent_id,
                kind=kind,
                title=title,
                instructions=instructions,
                expected_output=expected_output,
                artifacts=artifacts or [],
                cycle=cycle,
            ),
            result_type=CreatedEngineeringChildTask,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ACTIVITY_RETRY,
        )
        delegated: DelegatedTaskResult = await workflow.execute_child_workflow(
            "DelegatedTaskWorkflow",
            DelegatedTaskInput(
                workspace_id=workspace_id,
                parent_task_id=parent_task_id,
                child_task_id=created.child_task_id,
                agent_id=target_agent_id,
                delegating_agent_id=delegated_by_agent_id,
                parent_run_id="",
                kind=kind,
                # Template-driven: there is no parked parent transcript to
                # stitch an observation into, so the summary message stays
                # model-visible on the parent task.
                blocking=False,
            ),
            id=f"delegated-{created.child_task_id}",
            result_type=DelegatedTaskResult,
        )
        return delegated
