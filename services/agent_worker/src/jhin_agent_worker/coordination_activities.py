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
- ``commit_agent_step`` → for each executed ``organization.ask_person`` call
  that reached somebody, lift it with :func:`person_question_ask_from_output`
  into ``StepResult.person_questions`` so the workflow holds the turn open
  for the answer, and suppress that call's ``tool_result`` message — the
  answer is written later, once, by ``deliver_question_answer``.
- ``commit_agent_step`` → for each executed work-request call, lift the
  outcome with :func:`work_request_start_from_output` into
  ``StepResult.work_request_starts`` — carrying which *side* of the ask the
  running agent is on, since only the requester parks on the answer; for
  each executed ``organization.review.submit`` call, lift it with
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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_db.models import Agent, AgentCapabilityGrant, ReviewPolicy, Task
from jhin_domain import ReviewMode, TaskState
from jhin_observability import get_logger
from jhin_policy import GrantEffect
from jhin_tools.ask_person import asked_question_id
from jhin_tools.directory import build_roster, render_roster
from jhin_tools.reviews import open_periodic_review
from jhin_tools.rollups import build_manager_rollup, render_manager_rollup
from jhin_tools.work_requests import finalize_work_request, note_unanswered_work_request
from jhin_workflows.agent_task.shared import (
    ACTIVITY_MARK_TASK_PAUSED,
    WORK_REQUEST_SIDE_REQUESTER,
    WORK_REQUEST_SIDE_RESPONDER,
    MarkTaskPausedInput,
    PersonQuestionAsk,
    ReviewDecisionSignal,
    WorkRequestStart,
)
from jhin_workflows.memory_maintenance import (
    SOURCE_KIND_MESSAGE,
    MemoryMaintenanceInput,
    start_memory_maintenance,
)
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
    ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED,
    FinalizeWorkRequestInput,
    NoteWorkRequestUnansweredInput,
)

logger = get_logger(__name__)
_WINDOW_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# Which side of the ask each tool speaks for. This is the authority for
# ``WorkRequestStart.side``, and it is a structural fact rather than a guess:
# ``organization.request_work`` is the requester's own tool, and
# ``organization.respond_work_request`` is limited by its validator to the
# request's target. Only the requester may then park on the answer.
_WORK_REQUEST_SIDE_BY_TOOL = {
    "organization.request_work": WORK_REQUEST_SIDE_REQUESTER,
    "organization.respond_work_request": WORK_REQUEST_SIDE_RESPONDER,
}


def work_request_start_from_output(
    output: dict[str, Any] | None, *, tool_name: str
) -> WorkRequestStart | None:
    """Lift an executed work-request tool output into the workflow contract
    when it created a task (an accept, or an auto-activated request); None
    for any other tool or outcome."""
    side = _WORK_REQUEST_SIDE_BY_TOOL.get(tool_name)
    if side is None or not output:
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
        side=side,
    )


def person_question_ask_from_output(
    output: dict[str, Any] | None, *, tool_name: str
) -> PersonQuestionAsk | None:
    """Lift an executed ask that reached somebody into the workflow contract.

    The decision itself lives in :func:`jhin_tools.ask_person.asked_question_id`,
    which the tool worker reads too: the projection's suppression, its lift,
    and the tool worker's ``stop_reason`` must never disagree about whether a
    step parked, and the tool worker cannot import this service.
    """
    question_id = asked_question_id(output, tool_name=tool_name)
    if not question_id:
        return None
    return PersonQuestionAsk(question_id=question_id)


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
    """Roster block for the running agent's prompt (public identity only).

    The agent's allowed capability patterns are read alongside the roster,
    but only to shape presentation (see :func:`render_roster`): agent ids
    are printed only for an agent that has a tool taking one, and the
    "look it up" hint only for one that can search the directory. The
    gateway remains the sole authorization check.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        return ""
    capabilities = list(
        await session.scalars(
            select(AgentCapabilityGrant.capability).where(
                AgentCapabilityGrant.workspace_id == workspace_id,
                AgentCapabilityGrant.agent_id == agent_id,
                AgentCapabilityGrant.effect == GrantEffect.ALLOW.value,
            )
        )
    )
    return render_roster(await build_roster(session, agent), capabilities=capabilities)


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
    def __init__(self, resources: Resources, temporal_client: TemporalClient | None = None) -> None:
        self._resources = resources
        self._temporal_client = temporal_client

    @activity.defn(name=ACTIVITY_FINALIZE_WORK_REQUEST)
    async def finalize_work_request_activity(self, params: FinalizeWorkRequestInput) -> str:
        """Terminal projection for WorkRequestTaskWorkflow. Idempotent."""
        requester_agent_id = ""
        requester_task_id = ""
        conversation_id = ""
        result_message_id = ""
        async with self._resources.session_factory() as session:
            request = await finalize_work_request(
                session,
                workspace_id=UUID(params.workspace_id),
                request_id=UUID(params.work_request_id),
                run_status=params.run_status,
            )
            await session.commit()
            status = request.status if request is not None else "missing"
            if request is not None:
                requester_agent_id = str(request.requester_agent_id)
                requester_task_id = (
                    str(request.requester_task_id) if request.requester_task_id else ""
                )
                conversation_id = str(request.conversation_id) if request.conversation_id else ""
                result_message_id = str(request.metadata_json.get("result_message_id", "") or "")
        logger.info(
            "work_request.finalized",
            work_request_id=params.work_request_id,
            task_id=params.task_id,
            run_status=params.run_status,
            request_status=status,
        )
        # The REQUESTER agent learns from the reported result (detached,
        # best-effort, idempotent on the result message id).
        if self._temporal_client is not None and requester_agent_id and result_message_id:
            try:
                start_status, _handle = await start_memory_maintenance(
                    self._temporal_client,
                    MemoryMaintenanceInput(
                        workspace_id=params.workspace_id,
                        agent_id=requester_agent_id,
                        source_kind=SOURCE_KIND_MESSAGE,
                        source_id=result_message_id,
                        task_id=requester_task_id,
                        conversation_id=conversation_id,
                    ),
                )
                logger.info("memory.maintenance_start", status=start_status)
            except Exception as error:
                logger.warning("memory.maintenance_start_failed", error_type=type(error).__name__)
        return status

    @activity.defn(name=ACTIVITY_NOTE_WORK_REQUEST_UNANSWERED)
    async def note_work_request_unanswered_activity(
        self, params: NoteWorkRequestUnansweredInput
    ) -> str:
        """The requester's bounded wait elapsed. Idempotent; Postgres is the
        authority — a request that finished while the timer was firing gets
        no "still waiting" line."""
        async with self._resources.session_factory() as session:
            outcome = await note_unanswered_work_request(
                session,
                UUID(params.workspace_id),
                UUID(params.work_request_id),
            )
            await session.commit()
        logger.info(
            "work_request.unanswered",
            work_request_id=params.work_request_id,
            outcome=outcome,
        )
        return outcome

    @activity.defn(name=ACTIVITY_MARK_TASK_PAUSED)
    async def mark_task_paused_activity(self, params: MarkTaskPausedInput) -> str:
        """Write the pause the run is actually observing.

        Only ever moves between running and paused: a task that reached a
        terminal state while the signal was in flight keeps its real outcome.
        """
        want = TaskState.PAUSED.value if params.paused else TaskState.RUNNING.value
        allowed = (
            (TaskState.RUNNING.value, TaskState.QUEUED.value)
            if params.paused
            else (TaskState.PAUSED.value,)
        )
        async with self._resources.session_factory() as session:
            await session.execute(
                update(Task)
                .where(
                    Task.id == UUID(params.task_id),
                    Task.workspace_id == UUID(params.workspace_id),
                    Task.state.in_(allowed),
                )
                .values(state=want)
            )
            await session.commit()
            changed = (
                await session.scalar(select(Task.state).where(Task.id == UUID(params.task_id)))
            ) == want
        logger.info("task.pause_observed", task_id=params.task_id, paused=params.paused)
        return "updated" if changed else "unchanged"

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
