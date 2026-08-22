"""Coordination activities (work requests, reviews, manager context) for the
agent worker. Kept in their own module so the Phase 10 rewrite of
``activities.py`` can adopt them without merge conflicts.

Integration points for ``AgentActivities`` (documented in
``docs/architecture/coordination.md``):

- ``run_agent_step_activity`` → after composing the task context, call
  :func:`organization_context` and :func:`manager_context` and pass the text
  into ``TaskContext(organization_context=..., manager_context=...)``.
- the tool worker's gateway evaluates :func:`jhin_tools.reviews.check_review_gate`
  before executing an authorized call; ``wait_review`` persists the call as
  ``pending_review`` and ``commit_agent_step`` lifts it into
  ``StepResult.waiting_review_id`` so the workflow parks; ``blocked`` is a
  recorded denial carrying the feedback.
- ``commit_agent_step`` → for each executed ``organization.respond_work_request``
  call, lift the outcome with :func:`work_request_start_from_output` into
  ``StepResult.work_request_starts``; for each executed
  ``organization.review.submit`` call, lift it with
  :func:`review_decision_from_output` into ``StepResult.review_decisions`` so
  the workflow signals the source task (both ride in the committed bundle so
  replays keep them).
- ``PeriodicReviewWorkflow`` → :meth:`CoordinationActivities.load_periodic_review_policy_activity`
  and :meth:`CoordinationActivities.open_periodic_review_activity`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_db.models import Agent, ReviewPolicy
from jhin_domain import ReviewMode
from jhin_observability import get_logger
from jhin_tools.directory import build_roster, render_roster
from jhin_tools.reviews import open_periodic_review
from jhin_tools.rollups import build_manager_rollup, render_manager_rollup
from jhin_tools.work_requests import finalize_work_request
from jhin_workflows.agent_task.shared import ReviewDecisionSignal, WorkRequestStart
from jhin_workflows.periodic_review import (
    ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY,
    ACTIVITY_OPEN_PERIODIC_REVIEW,
    OpenPeriodicReviewInput,
    OpenPeriodicReviewResult,
    PeriodicReviewInput,
    PeriodicReviewPolicyState,
)
from jhin_workflows.work_request_task import (
    ACTIVITY_FINALIZE_WORK_REQUEST,
    FinalizeWorkRequestInput,
)

logger = get_logger(__name__)
_WINDOW_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def work_request_start_from_output(output: dict[str, Any] | None) -> WorkRequestStart | None:
    """Lift an executed ``organization.respond_work_request`` output into the
    workflow contract when it created a task (accept); None otherwise."""
    if not output:
        return None
    task_id = output.get("created_task_id")
    request_id = output.get("work_request_id")
    if not isinstance(task_id, str) or not task_id or not isinstance(request_id, str):
        return None
    agent_id = output.get("agent_id")
    return WorkRequestStart(
        work_request_id=request_id,
        task_id=task_id,
        agent_id=str(agent_id) if isinstance(agent_id, str) else "",
    )


def review_decision_from_output(output: dict[str, Any] | None) -> ReviewDecisionSignal | None:
    """Lift an executed ``organization.review.submit`` output into the
    workflow contract when it names a source workflow to wake; None
    otherwise (reviews without a task park nothing)."""
    if not output:
        return None
    review_id = output.get("review_id")
    status = output.get("status")
    workflow_id = output.get("source_workflow_id")
    if not (isinstance(review_id, str) and review_id and isinstance(status, str)):
        return None
    if not isinstance(workflow_id, str) or not workflow_id:
        return None
    return ReviewDecisionSignal(
        review_id=review_id, status=status, source_workflow_id=workflow_id[:200]
    )


def _parse_window(raw: str, *, field: str) -> datetime:
    try:
        return datetime.strptime(raw, _WINDOW_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise ApplicationError(
            f"invalid periodic review {field}", type="periodic_window_invalid", non_retryable=True
        ) from error


async def organization_context(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> str:
    """Roster block for the running agent's prompt (public identity only)."""
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        return ""
    return render_roster(await build_roster(session, agent))


async def manager_context(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> str:
    """Rollup block for a manager's prompt; empty for agents with no reports.
    Only the current reporting manager receives it — the rollup is built
    from the agent's own manager chain, never from a requested agent id."""
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        return ""
    return render_manager_rollup(await build_manager_rollup(session, agent))


class CoordinationActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    @activity.defn(name=ACTIVITY_FINALIZE_WORK_REQUEST)
    async def finalize_work_request_activity(self, params: FinalizeWorkRequestInput) -> str:
        """Terminal projection for WorkRequestTaskWorkflow. Idempotent."""
        async with self._resources.session_factory() as session:
            request = await finalize_work_request(
                session,
                workspace_id=UUID(params.workspace_id),
                request_id=UUID(params.work_request_id),
                run_status=params.run_status,
            )
            await session.commit()
            status = request.status if request is not None else "missing"
        logger.info(
            "work_request.finalized",
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status=params.run_status,
            request_status=status,
        )
        return status

    @activity.defn(name=ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY)
    async def load_periodic_review_policy_activity(
        self, params: PeriodicReviewInput
    ) -> PeriodicReviewPolicyState:
        """Current facts for one periodic policy; the workflow exits when the
        policy is gone, disabled, or no longer periodic."""
        async with self._resources.session_factory() as session:
            policy = await session.scalar(
                select(ReviewPolicy).where(
                    ReviewPolicy.id == UUID(params.policy_id),
                    ReviewPolicy.workspace_id == UUID(params.workspace_id),
                )
            )
            if policy is None:
                return PeriodicReviewPolicyState(exists=False, enabled=False, period_seconds=0)
            return PeriodicReviewPolicyState(
                exists=True,
                enabled=bool(policy.enabled) and policy.mode == ReviewMode.PERIODIC.value,
                period_seconds=int(policy.period_seconds or 0),
            )

    @activity.defn(name=ACTIVITY_OPEN_PERIODIC_REVIEW)
    async def open_periodic_review_activity(
        self, params: OpenPeriodicReviewInput
    ) -> OpenPeriodicReviewResult:
        """Open the one work_review for a closed window. Idempotent: a retry
        or a duplicate window finds the review by its trigger key."""
        window_start = _parse_window(params.window_start, field="window_start")
        window_end = _parse_window(params.window_end, field="window_end")
        async with self._resources.session_factory() as session:
            policy = await session.scalar(
                select(ReviewPolicy).where(
                    ReviewPolicy.id == UUID(params.policy_id),
                    ReviewPolicy.workspace_id == UUID(params.workspace_id),
                )
            )
            if policy is None:
                return OpenPeriodicReviewResult(
                    review_id=None, status="policy_missing", created=False
                )
            if not policy.enabled or policy.mode != ReviewMode.PERIODIC.value:
                return OpenPeriodicReviewResult(
                    review_id=None, status="policy_disabled", created=False
                )
            review, created = await open_periodic_review(
                session, policy, window_start=window_start, window_end=window_end
            )
            await session.commit()
            result = OpenPeriodicReviewResult(
                review_id=str(review.id), status=review.status, created=created
            )
        logger.info(
            "periodic_review.window",
            policy_id=params.policy_id,
            review_id=result.review_id,
            status=result.status,
            created=created,
        )
        return result
