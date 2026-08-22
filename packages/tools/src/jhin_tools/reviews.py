"""Work reviews: exception-based AI/human oversight that composes with — and
never replaces — the tool gateway and human approvals.

Gate order for a tool call (documented in ``docs/architecture/coordination.md``):
capability/scope/validator → ``check_review_gate`` (this module) → human
approval → execution. A review verdict can only let the gateway continue
evaluating; it cannot synthesize a grant, waive an explicit deny, or decide
an approval reserved for a human.

Every triggered review is keyed by a deterministic ``trigger_key`` so one
exception yields at most one ``work_review`` row, and reviewer resolution is
the pure :func:`jhin_policy.resolve_reviewer` over candidates loaded here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import (
    Agent,
    AgentRun,
    AgentTeamMembership,
    AuditEvent,
    ReviewPolicy,
    Task,
    WorkReview,
)
from jhin_domain import (
    REVIEW_BLOCKING_MODES,
    ActorType,
    AgentStatus,
    ReviewerType,
    ReviewMode,
    ReviewVerdict,
    WorkReviewStatus,
    new_uuid7,
)
from jhin_policy import (
    REVIEW_REQUEST_CAPABILITY,
    DecisionType,
    Grant,
    PolicyDecision,
    ReviewContext,
    ReviewerCandidates,
    ReviewerSelector,
    ReviewPolicySpec,
    ReviewRequirement,
    RiskLevel,
    ToolDefinition,
    evaluate_review_policies,
    policy_spec_from_row,
    resolve_reviewer,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator

GateStatus = Literal["proceed", "wait_review", "blocked"]

_VERDICT_STATUS: dict[str, str] = {
    ReviewVerdict.APPROVE.value: WorkReviewStatus.APPROVED.value,
    ReviewVerdict.CHANGES_REQUESTED.value: WorkReviewStatus.CHANGES_REQUESTED.value,
    ReviewVerdict.ESCALATE.value: WorkReviewStatus.ESCALATED.value,
}


def _now() -> datetime:
    return datetime.now(UTC)


class ToolCallIntent(BaseModel):
    """What the worker is about to execute (already authorized upstream)."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    risk: str | None = None
    tool_call_id: UUID | None = None
    # Outcome facts for post-action evaluation.
    tool_failed: bool = False
    approval_denied: bool = False
    policy_denied: bool = False


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: GateStatus
    code: str
    reason: str
    review_id: UUID | None = None
    reviewer_type: str | None = None
    reviewer_agent_id: UUID | None = None
    feedback: str = ""


class ReviewError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- loading ---


async def load_policy_specs(session: AsyncSession, workspace_id: UUID) -> list[ReviewPolicySpec]:
    rows = await session.scalars(
        select(ReviewPolicy).where(
            ReviewPolicy.workspace_id == workspace_id, ReviewPolicy.enabled.is_(True)
        )
    )
    specs: list[ReviewPolicySpec] = []
    for row in rows:
        spec = policy_spec_from_row(
            policy_id=str(row.id),
            mode=row.mode,
            scope_kind=row.scope_kind,
            scope_id=str(row.scope_id) if row.scope_id else None,
            scope_key=row.scope_key,
            enabled=row.enabled,
            conditions_json=row.conditions_json,
            reviewer_selector_json=row.reviewer_selector_json,
            fail_closed=row.fail_closed,
            priority=row.priority,
        )
        if spec is not None:
            specs.append(spec)
    return specs


async def agent_team_ids(
    session: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> tuple[str, ...]:
    teams: set[str] = set()
    agent = await session.get(Agent, agent_id)
    if agent is not None and agent.team_id is not None:
        teams.add(str(agent.team_id))
    rows = await session.scalars(
        select(AgentTeamMembership.team_id).where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id == agent_id,
            AgentTeamMembership.left_at.is_(None),
        )
    )
    teams.update(str(t) for t in rows)
    return tuple(sorted(teams))


def task_type_of(task: Task | None) -> str | None:
    if task is None:
        return None
    raw = task.metadata_json.get("task_type") or task.metadata_json.get("origin")
    return str(raw) if isinstance(raw, str) and raw else None


async def reviewer_candidates(
    session: AsyncSession, workspace_id: UUID, subject_agent_id: UUID, selector: ReviewerSelector
) -> ReviewerCandidates:
    """Load only the candidates the selector can name; never the whole org."""
    subject = await session.get(Agent, subject_agent_id)
    wanted: set[UUID] = set()
    manager_id = subject.manager_agent_id if subject is not None else None
    if manager_id is not None:
        wanted.add(manager_id)
    for raw in (selector.agent_id, selector.fallback_agent_id):
        if raw:
            try:
                wanted.add(UUID(raw))
            except ValueError:
                continue
    role_agents: dict[str, tuple[str, ...]] = {}
    if selector.kind == "team_role" and selector.role_label:
        holders = await session.scalars(
            select(AgentTeamMembership.agent_id).where(
                AgentTeamMembership.workspace_id == workspace_id,
                AgentTeamMembership.role_label == selector.role_label,
                AgentTeamMembership.left_at.is_(None),
            )
        )
        holder_ids = [h for h in holders if h != subject_agent_id]
        wanted.update(holder_ids)
        role_agents[selector.role_label] = tuple(str(h) for h in holder_ids)
    active: dict[str, bool] = {}
    if wanted:
        rows = await session.execute(
            select(Agent.id, Agent.status).where(
                Agent.workspace_id == workspace_id, Agent.id.in_(list(wanted))
            )
        )
        active = {str(i): s == AgentStatus.ACTIVE.value for i, s in rows.all()}
    # An agent never reviews its own work.
    active.pop(str(subject_agent_id), None)
    return ReviewerCandidates(
        manager_agent_id=str(manager_id) if manager_id else None,
        active_agents=active,
        team_role_agents=role_agents,
    )


# --- creation ---


async def get_review(
    session: AsyncSession, workspace_id: UUID, review_id: UUID
) -> WorkReview | None:
    review: WorkReview | None = await session.scalar(
        select(WorkReview).where(
            WorkReview.id == review_id, WorkReview.workspace_id == workspace_id
        )
    )
    return review


async def open_review(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    subject_agent_id: UUID,
    trigger_key: str,
    mode: ReviewMode,
    selector: ReviewerSelector,
    fail_closed: bool,
    policy_id: UUID | None = None,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
    tool_call_id: UUID | None = None,
    work_request_id: UUID | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[WorkReview, bool]:
    """Create (or return) the one review for ``trigger_key`` and resolve its
    reviewer deterministically. Returns ``(review, created)``."""
    existing = await session.scalar(
        select(WorkReview).where(
            WorkReview.workspace_id == workspace_id, WorkReview.trigger_key == trigger_key
        )
    )
    if existing is not None:
        return existing, False
    candidates = await reviewer_candidates(session, workspace_id, subject_agent_id, selector)
    resolution = resolve_reviewer(selector, candidates, mode=mode, fail_closed=fail_closed)
    status = WorkReviewStatus.PENDING.value
    reviewer_type = "none"
    reviewer_agent_id: UUID | None = None
    if resolution.outcome == "resolved" and resolution.reviewer_agent_id is not None:
        reviewer_type = ReviewerType.AGENT.value
        reviewer_agent_id = UUID(resolution.reviewer_agent_id)
    elif resolution.outcome in ("human_required", "fail_closed"):
        # Fail-closed reviews still surface to humans as an attention item;
        # they block until a human decides.
        reviewer_type = ReviewerType.HUMAN.value
    else:
        status = WorkReviewStatus.SKIPPED.value
    review = WorkReview(
        id=new_uuid7(),
        workspace_id=workspace_id,
        policy_id=policy_id,
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        work_request_id=work_request_id,
        subject_agent_id=subject_agent_id,
        trigger_key=trigger_key[:300],
        mode=mode.value,
        evidence_json={
            **(evidence or {}),
            "resolution": resolution.code,
            "fail_closed": resolution.outcome == "fail_closed",
        },
        reviewer_type=reviewer_type,
        reviewer_agent_id=reviewer_agent_id,
        status=status,
        requested_at=_now(),
        decided_at=_now() if status == WorkReviewStatus.SKIPPED.value else None,
    )
    session.add(review)
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type=ActorType.SYSTEM.value,
            actor_id=None,
            action="work_review.opened",
            target_type="work_review",
            target_id=review.id,
            metadata_json={
                "mode": mode.value,
                "status": status,
                "reviewer_type": reviewer_type,
                "reviewer_agent_id": str(reviewer_agent_id) if reviewer_agent_id else None,
                "resolution": resolution.code,
                "policy_id": str(policy_id) if policy_id else None,
                "run_id": str(run_id) if run_id else None,
            },
        )
    )
    await session.flush()
    return review, True


def _gate_from_review(review: WorkReview, *, blocking: bool, code: str, reason: str) -> GateResult:
    if review.status == WorkReviewStatus.APPROVED.value or not blocking:
        return GateResult(
            status="proceed",
            code="review_approved" if review.status == WorkReviewStatus.APPROVED.value else code,
            reason=reason,
            review_id=review.id,
            reviewer_type=review.reviewer_type,
            reviewer_agent_id=review.reviewer_agent_id,
            feedback=review.feedback,
        )
    if review.status == WorkReviewStatus.SKIPPED.value:
        return GateResult(
            status="proceed", code="review_skipped", reason=reason, review_id=review.id
        )
    if review.status == WorkReviewStatus.PENDING.value:
        return GateResult(
            status="wait_review",
            code=code,
            reason=reason,
            review_id=review.id,
            reviewer_type=review.reviewer_type,
            reviewer_agent_id=review.reviewer_agent_id,
        )
    return GateResult(
        status="blocked",
        code=f"review_{review.status}",
        reason=review.feedback or f"review outcome: {review.status}",
        review_id=review.id,
        reviewer_type=review.reviewer_type,
        reviewer_agent_id=review.reviewer_agent_id,
        feedback=review.feedback,
    )


async def evaluate_review_event(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    subject_agent_id: UUID,
    mode: ReviewMode,
    trigger_scope: str,
    task: Task | None = None,
    run: AgentRun | None = None,
    tool_call_id: UUID | None = None,
    work_request_id: UUID | None = None,
    facts: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> GateResult:
    """Evaluate enabled policies of ``mode`` for this moment; open at most one
    review per matched policy (keyed ``{mode}:{trigger_scope}:{policy}``) and
    return the gate outcome of the highest-priority requirement."""
    specs = await load_policy_specs(session, workspace_id)
    if not specs:
        return GateResult(status="proceed", code="no_review", reason="no review policies")
    context = ReviewContext(
        mode=mode,
        agent_id=str(subject_agent_id),
        team_ids=await agent_team_ids(session, workspace_id, subject_agent_id),
        task_type=task_type_of(task),
        cost_micros=run.estimated_cost_micros if run is not None else 0,
        total_tokens=(run.input_tokens + run.output_tokens) if run is not None else 0,
        elapsed_seconds=(
            int((_now() - run.started_at).total_seconds())
            if run is not None and run.started_at is not None
            else 0
        ),
        **(facts or {}),
    )
    decision = evaluate_review_policies(specs, context)
    if not decision.required:
        return GateResult(status="proceed", code=decision.code, reason=decision.reason)
    results: list[GateResult] = []
    for requirement in decision.requirements:
        review, _ = await open_review(
            session,
            workspace_id=workspace_id,
            subject_agent_id=subject_agent_id,
            trigger_key=f"{mode.value}:{trigger_scope}:{requirement.policy_id}",
            mode=mode,
            selector=requirement.reviewer,
            fail_closed=requirement.fail_closed,
            policy_id=UUID(requirement.policy_id),
            task_id=task.id if task is not None else None,
            run_id=run.id if run is not None else None,
            tool_call_id=tool_call_id,
            work_request_id=work_request_id,
            evidence={
                **(evidence or {}),
                "matched_conditions": [k.value for k in requirement.matched_conditions],
            },
        )
        results.append(
            _gate_from_review(
                review,
                blocking=decision.blocking,
                code=decision.code,
                reason=decision.reason,
            )
        )
    # Most restrictive outcome wins: blocked > wait_review > proceed.
    order = {"blocked": 0, "wait_review": 1, "proceed": 2}
    return sorted(results, key=lambda r: order[r.status])[0]


async def check_review_gate(
    session: AsyncSession, run: AgentRun, intent: ToolCallIntent
) -> GateResult:
    """Worker integration point: call *after* the gateway authorized a tool
    call and *before* executing it (and before human approval staging).

    ``proceed`` → continue the normal gateway pipeline; ``wait_review`` →
    park the run on ``review_id`` (the workflow resumes when the review is
    decided and must re-run the full authorization chain); ``blocked`` → the
    call is not executed and ``feedback`` returns to the model.
    """
    task = await session.get(Task, run.task_id) if run.task_id is not None else None
    scope = (
        str(intent.tool_call_id)
        if intent.tool_call_id is not None
        else f"{run.id}:{intent.tool_name}"
    )
    return await evaluate_review_event(
        session,
        workspace_id=run.workspace_id,
        subject_agent_id=run.agent_id,
        mode=ReviewMode.PRE_ACTION,
        trigger_scope=scope,
        task=task,
        run=run,
        tool_call_id=intent.tool_call_id,
        facts={
            "risk": intent.risk,
            "tool_failed": intent.tool_failed,
            "approval_denied": intent.approval_denied,
            "policy_denied": intent.policy_denied,
        },
        evidence={"tool_name": intent.tool_name, "risk": intent.risk},
    )


async def decide_review(
    session: AsyncSession,
    review: WorkReview,
    *,
    verdict: str,
    feedback: str,
    decided_by_user_id: UUID | None = None,
    decided_by_agent_id: UUID | None = None,
) -> WorkReview:
    """Compare-and-set: the first decision wins; retries return it unchanged."""
    if review.status != WorkReviewStatus.PENDING.value:
        return review
    status = _VERDICT_STATUS.get(verdict)
    if status is None:
        raise ReviewError("invalid_verdict", f"unknown verdict '{verdict}'")
    review.status = status
    review.verdict = verdict
    review.feedback = feedback[:4_000]
    review.decided_at = _now()
    review.decided_by_user_id = decided_by_user_id
    review.decided_by_agent_id = decided_by_agent_id
    session.add(
        AuditEvent(
            workspace_id=review.workspace_id,
            actor_type=(ActorType.USER if decided_by_user_id else ActorType.AGENT).value,
            actor_id=decided_by_user_id or decided_by_agent_id,
            action="work_review.decided",
            target_type="work_review",
            target_id=review.id,
            metadata_json={"verdict": verdict, "status": status, "mode": review.mode},
        )
    )
    await session.flush()
    return review


# --- gateway tools ---


class ReviewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=100)
    id: str = Field(default="", max_length=300)
    url_ref: str = Field(default="", max_length=1000)


class RequestReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    artifacts: list[ReviewArtifact] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)


class RequestReviewOutput(BaseModel):
    review_id: str | None
    status: str
    reviewer_type: str | None = None
    reviewer_agent_id: str | None = None
    reviewer_agent_name: str | None = None
    detail: str = ""


class SubmitReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["approve", "changes_requested", "escalate"]
    feedback: str = Field(min_length=1, max_length=4_000)


class SubmitReviewOutput(BaseModel):
    review_id: str
    status: str
    verdict: str


async def _request_review(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    """An agent explicitly asks for a review of its current work. Matching
    before-close policies decide the reviewer; with none, the default
    selector (reporting manager → human) applies."""
    data = cast(RequestReviewInput, payload)
    task = await ctx.session.get(Task, ctx.task_id)
    run = await ctx.session.get(AgentRun, ctx.run_id)
    evidence = {
        "summary": data.summary,
        "artifacts": [a.model_dump() for a in data.artifacts],
        "risks": list(data.risks),
    }
    scope = str(ctx.tool_call_id or new_uuid7())
    gate = await evaluate_review_event(
        ctx.session,
        workspace_id=ctx.workspace_id,
        subject_agent_id=ctx.agent_id,
        mode=ReviewMode.BEFORE_CLOSE,
        trigger_scope=scope,
        task=task,
        run=run,
        tool_call_id=ctx.tool_call_id,
        facts={"explicit_request": True},
        evidence=evidence,
    )
    if gate.review_id is None:
        review, _ = await open_review(
            ctx.session,
            workspace_id=ctx.workspace_id,
            subject_agent_id=ctx.agent_id,
            trigger_key=f"explicit:{scope}",
            mode=ReviewMode.BEFORE_CLOSE,
            selector=ReviewerSelector(),
            fail_closed=False,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            evidence={**evidence, "matched_conditions": ["explicit_request"]},
        )
    else:
        loaded = await get_review(ctx.session, ctx.workspace_id, gate.review_id)
        if loaded is None:
            raise ValueError("review disappeared before execution")
        review = loaded
    reviewer_name: str | None = None
    if review.reviewer_agent_id is not None:
        reviewer = await ctx.session.get(Agent, review.reviewer_agent_id)
        reviewer_name = reviewer.name if reviewer is not None else None
    return RequestReviewOutput(
        review_id=str(review.id),
        status=review.status,
        reviewer_type=review.reviewer_type,
        reviewer_agent_id=str(review.reviewer_agent_id) if review.reviewer_agent_id else None,
        reviewer_agent_name=reviewer_name,
        detail=(
            "review skipped: no reviewer is available"
            if review.status == WorkReviewStatus.SKIPPED.value
            else "review requested; continue or finish your work while it is pending"
        ),
    )


async def validate_submit_review(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Structural: only the resolved AI reviewer may submit, while pending."""
    data = cast(SubmitReviewInput, payload)
    try:
        review_id = UUID(data.review_id)
    except ValueError:
        return PolicyDecision(
            decision=DecisionType.DENY, code="review_not_found", reason="no such review"
        )
    review = await get_review(ctx.session, ctx.workspace_id, review_id)
    if review is None:
        return PolicyDecision(
            decision=DecisionType.DENY, code="review_not_found", reason="no such review"
        )
    if review.reviewer_type != ReviewerType.AGENT.value or review.reviewer_agent_id != ctx.agent_id:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="not_assigned_reviewer",
            reason="only the assigned AI reviewer may submit this review",
        )
    if review.status != WorkReviewStatus.PENDING.value:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="review_already_decided",
            reason=f"review is already {review.status}",
        )
    return None


async def _submit_review(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(SubmitReviewInput, payload)
    review = await get_review(ctx.session, ctx.workspace_id, UUID(data.review_id))
    if review is None:
        raise ValueError("review disappeared before execution")
    review = await decide_review(
        ctx.session,
        review,
        verdict=data.verdict,
        feedback=data.feedback,
        decided_by_agent_id=ctx.agent_id,
    )
    return SubmitReviewOutput(
        review_id=str(review.id), status=review.status, verdict=review.verdict or data.verdict
    )


REVIEW_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.review.request",
            description=(
                "Ask for a review of your current work (summary, artifacts, "
                "risks). Your workspace's review policies pick the reviewer — "
                "usually your manager, a named agent, or a human."
            ),
            risk=RiskLevel.WRITE,
            input_model=RequestReviewInput,
            output_model=RequestReviewOutput,
            required_capability=REVIEW_REQUEST_CAPABILITY,
            supports_approval=True,
        ),
        _request_review,
        None,
    ),
    (
        ToolDefinition(
            name="organization.review.submit",
            description=(
                "Submit your verdict on a review assigned to you: approve, "
                "changes_requested, or escalate, with concise feedback "
                "(decision summary, evidence references, risks, next action)."
            ),
            risk=RiskLevel.WRITE,
            input_model=SubmitReviewInput,
            output_model=SubmitReviewOutput,
            required_capability=REVIEW_REQUEST_CAPABILITY,
            supports_approval=True,
        ),
        _submit_review,
        validate_submit_review,
    ),
)


def blocking_mode(mode: str) -> bool:
    try:
        return ReviewMode(mode) in REVIEW_BLOCKING_MODES
    except ValueError:
        return False


__all__ = [
    "REVIEW_TOOLS",
    "GateResult",
    "ReviewError",
    "ReviewRequirement",
    "ToolCallIntent",
    "blocking_mode",
    "check_review_gate",
    "decide_review",
    "evaluate_review_event",
    "get_review",
    "load_policy_specs",
    "open_review",
]
