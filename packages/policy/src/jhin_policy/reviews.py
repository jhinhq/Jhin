"""Pure review-policy evaluation and deterministic reviewer resolution.

Review policies are exception-based oversight: routine low-risk work
proceeds without a manager; a policy fires only when one of its conditions
matches the facts of the moment (``ReviewContext``). Each match yields a
``ReviewRequirement`` naming the reviewer selector; the caller resolves the
selector against already-authorized candidates with :func:`resolve_reviewer`.

Invariants:

- a review gates and records; it can never approve a human-reserved
  approval nor override tool policy (the gateway stays the authority);
- a mandatory (``fail_closed``) pre-action/before-close policy with no
  resolvable reviewer fails closed; optional policies are skipped;
- reporting relationships are routing context: a manager is a reviewer
  *candidate* only because the selector says so, never by implication.

This module is pure (no I/O).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jhin_domain import REVIEW_BLOCKING_MODES, ReviewMode, ReviewScopeKind

REVIEW_REQUEST_CAPABILITY = "organization.review.request"


class ReviewConditionKind(StrEnum):
    ELEVATED_ACTION = "elevated_action"
    DESTRUCTIVE_ACTION = "destructive_action"
    COST_THRESHOLD = "cost_threshold"
    TOKEN_THRESHOLD = "token_threshold"
    TIME_THRESHOLD = "time_threshold"
    TOOL_FAILURE = "tool_failure"
    TEST_FAILURE = "test_failure"
    APPROVAL_DENIED = "approval_denied"
    POLICY_DENIED = "policy_denied"
    BLOCKED = "blocked"
    LOW_CONFIDENCE = "low_confidence"
    CROSS_TEAM_REQUEST = "cross_team_request"
    EXPLICIT_REQUEST = "explicit_request"
    ALWAYS = "always"


_THRESHOLD_KINDS = frozenset(
    {
        ReviewConditionKind.COST_THRESHOLD,
        ReviewConditionKind.TOKEN_THRESHOLD,
        ReviewConditionKind.TIME_THRESHOLD,
    }
)


class ReviewCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ReviewConditionKind
    # cost: micro-dollars; tokens: count; time: seconds; confidence: 0..1.
    threshold: float | None = Field(default=None, ge=0)


ReviewerKind = Literal["reporting_manager", "agent", "team_role", "human"]


class ReviewerSelector(BaseModel):
    """Who reviews. ``agent_id``/``role_label`` accompany their kinds;
    fallbacks apply when the primary cannot be resolved (e.g. no manager)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ReviewerKind = "reporting_manager"
    agent_id: str | None = None
    role_label: str | None = None
    fallback_agent_id: str | None = None
    fallback_to_human: bool = True


class ReviewPolicySpec(BaseModel):
    """In-memory mirror of one review_policy row."""

    model_config = ConfigDict(frozen=True)

    policy_id: str
    mode: ReviewMode
    scope_kind: ReviewScopeKind = ReviewScopeKind.WORKSPACE
    scope_id: str | None = None
    scope_key: str | None = None
    enabled: bool = True
    conditions: tuple[ReviewCondition, ...] = ()
    reviewer: ReviewerSelector = Field(default_factory=ReviewerSelector)
    fail_closed: bool = False
    priority: int = 100


class ReviewContext(BaseModel):
    """Facts about the moment a review might be needed."""

    model_config = ConfigDict(frozen=True)

    mode: ReviewMode
    agent_id: str
    team_ids: tuple[str, ...] = ()
    task_type: str | None = None
    # Tool-call facts (pre/post action).
    risk: str | None = None
    tool_failed: bool = False
    test_failed: bool = False
    approval_denied: bool = False
    policy_denied: bool = False
    # Run/task facts.
    cost_micros: int = 0
    total_tokens: int = 0
    elapsed_seconds: int = 0
    blocked: bool = False
    confidence: float | None = None
    cross_team_request: bool = False
    explicit_request: bool = False


class ReviewRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str
    mode: ReviewMode
    matched_conditions: tuple[ReviewConditionKind, ...]
    reviewer: ReviewerSelector
    fail_closed: bool
    priority: int


class ReviewDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    required: bool
    blocking: bool
    requirements: tuple[ReviewRequirement, ...] = ()
    code: str = "no_review"
    reason: str = "no review policy matched"

    @property
    def primary(self) -> ReviewRequirement | None:
        return self.requirements[0] if self.requirements else None


_SCOPE_SPECIFICITY: dict[ReviewScopeKind, int] = {
    ReviewScopeKind.AGENT: 0,
    ReviewScopeKind.TASK_TYPE: 1,
    ReviewScopeKind.TEAM: 2,
    ReviewScopeKind.WORKSPACE: 3,
}


def _scope_applies(policy: ReviewPolicySpec, context: ReviewContext) -> bool:
    if policy.scope_kind is ReviewScopeKind.WORKSPACE:
        return True
    if policy.scope_kind is ReviewScopeKind.AGENT:
        return policy.scope_id == context.agent_id
    if policy.scope_kind is ReviewScopeKind.TEAM:
        return policy.scope_id is not None and policy.scope_id in context.team_ids
    return policy.scope_key is not None and policy.scope_key == context.task_type


def _condition_matches(condition: ReviewCondition, context: ReviewContext) -> bool:
    kind = condition.kind
    if kind is ReviewConditionKind.ALWAYS:
        return True
    if kind is ReviewConditionKind.ELEVATED_ACTION:
        return context.risk in ("elevated", "destructive")
    if kind is ReviewConditionKind.DESTRUCTIVE_ACTION:
        return context.risk == "destructive"
    if kind in _THRESHOLD_KINDS:
        if condition.threshold is None:
            return False
        value = {
            ReviewConditionKind.COST_THRESHOLD: context.cost_micros,
            ReviewConditionKind.TOKEN_THRESHOLD: context.total_tokens,
            ReviewConditionKind.TIME_THRESHOLD: context.elapsed_seconds,
        }[kind]
        return value >= condition.threshold
    if kind is ReviewConditionKind.TOOL_FAILURE:
        return context.tool_failed
    if kind is ReviewConditionKind.TEST_FAILURE:
        return context.test_failed
    if kind is ReviewConditionKind.APPROVAL_DENIED:
        return context.approval_denied
    if kind is ReviewConditionKind.POLICY_DENIED:
        return context.policy_denied
    if kind is ReviewConditionKind.BLOCKED:
        return context.blocked
    if kind is ReviewConditionKind.LOW_CONFIDENCE:
        if context.confidence is None:
            return False
        floor = condition.threshold if condition.threshold is not None else 0.5
        return context.confidence < floor
    if kind is ReviewConditionKind.CROSS_TEAM_REQUEST:
        return context.cross_team_request
    if kind is ReviewConditionKind.EXPLICIT_REQUEST:
        return context.explicit_request
    return False


def evaluate_review_policies(
    policies: Sequence[ReviewPolicySpec], context: ReviewContext
) -> ReviewDecision:
    """Match enabled policies of ``context.mode`` whose scope applies and at
    least one condition fires. Most specific scope first, then priority
    (lower wins), then policy id for determinism."""
    requirements: list[tuple[tuple[int, int, str], ReviewRequirement]] = []
    for policy in policies:
        if not policy.enabled or policy.mode is not context.mode:
            continue
        if not _scope_applies(policy, context):
            continue
        matched = tuple(c.kind for c in policy.conditions if _condition_matches(c, context))
        if not matched:
            continue
        requirement = ReviewRequirement(
            policy_id=policy.policy_id,
            mode=policy.mode,
            matched_conditions=matched,
            reviewer=policy.reviewer,
            fail_closed=policy.fail_closed,
            priority=policy.priority,
        )
        key = (_SCOPE_SPECIFICITY[policy.scope_kind], policy.priority, policy.policy_id)
        requirements.append((key, requirement))
    if not requirements:
        return ReviewDecision(
            required=False, blocking=False, code="no_review", reason="no review policy matched"
        )
    requirements.sort(key=lambda item: item[0])
    ordered = tuple(r for _, r in requirements)
    primary = ordered[0]
    return ReviewDecision(
        required=True,
        blocking=context.mode in REVIEW_BLOCKING_MODES,
        requirements=ordered,
        code="review_required",
        reason=(
            f"review policy {primary.policy_id} matched "
            f"{', '.join(k.value for k in primary.matched_conditions)}"
        ),
    )


# --- reviewer resolution ---


class ReviewerCandidates(BaseModel):
    """Already-authorized candidates loaded by the caller."""

    model_config = ConfigDict(frozen=True)

    manager_agent_id: str | None = None
    # agent id -> active (only agents that exist in the workspace appear).
    active_agents: dict[str, bool] = Field(default_factory=dict)
    # role label -> agent ids carrying it (team-role selectors).
    team_role_agents: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    human_available: bool = True


ResolutionOutcome = Literal["resolved", "human_required", "skipped", "fail_closed"]


class ReviewerResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ResolutionOutcome
    reviewer_agent_id: str | None = None
    code: str = ""
    reason: str = ""


def _active(candidates: ReviewerCandidates, agent_id: str | None) -> str | None:
    if agent_id is not None and candidates.active_agents.get(agent_id):
        return agent_id
    return None


def resolve_reviewer(
    selector: ReviewerSelector,
    candidates: ReviewerCandidates,
    *,
    mode: ReviewMode,
    fail_closed: bool,
) -> ReviewerResolution:
    """Deterministically pick a reviewer or decide what happens without one.

    Order: the selector's primary → ``fallback_agent_id`` → human (when
    ``fallback_to_human``) → skipped, except that a mandatory blocking
    policy fails closed instead of skipping.
    """
    primary: str | None = None
    if selector.kind == "reporting_manager":
        primary = _active(candidates, candidates.manager_agent_id)
    elif selector.kind == "agent":
        primary = _active(candidates, selector.agent_id)
    elif selector.kind == "team_role":
        holders = candidates.team_role_agents.get(selector.role_label or "", ())
        primary = next((a for a in sorted(holders) if _active(candidates, a)), None)
    elif selector.kind == "human" and candidates.human_available:
        return ReviewerResolution(
            outcome="human_required", code="human_reviewer", reason="policy names a human"
        )
    if primary is not None:
        return ReviewerResolution(
            outcome="resolved",
            reviewer_agent_id=primary,
            code="reviewer_resolved",
            reason=f"reviewer resolved via {selector.kind}",
        )
    fallback = _active(candidates, selector.fallback_agent_id)
    if fallback is not None:
        return ReviewerResolution(
            outcome="resolved",
            reviewer_agent_id=fallback,
            code="reviewer_fallback_agent",
            reason="primary reviewer unavailable; fallback agent selected",
        )
    if selector.fallback_to_human and candidates.human_available:
        return ReviewerResolution(
            outcome="human_required",
            code="reviewer_fallback_human",
            reason="no agent reviewer available; escalated to a human",
        )
    if fail_closed and mode in REVIEW_BLOCKING_MODES:
        return ReviewerResolution(
            outcome="fail_closed",
            code="mandatory_reviewer_missing",
            reason="mandatory review has no reviewer; the action is blocked",
        )
    return ReviewerResolution(
        outcome="skipped", code="reviewer_missing", reason="no reviewer available; review skipped"
    )


def policy_spec_from_row(
    *,
    policy_id: str,
    mode: str,
    scope_kind: str,
    scope_id: str | None,
    scope_key: str | None,
    enabled: bool,
    conditions_json: Any,
    reviewer_selector_json: Any,
    fail_closed: bool,
    priority: int,
) -> ReviewPolicySpec | None:
    """Build a spec from persisted JSON; malformed rows are ignored (they
    can never widen or block access by accident)."""
    try:
        conditions = tuple(
            ReviewCondition.model_validate(raw)
            for raw in (conditions_json if isinstance(conditions_json, list) else [])
        )
        reviewer = ReviewerSelector.model_validate(
            reviewer_selector_json if isinstance(reviewer_selector_json, dict) else {}
        )
        return ReviewPolicySpec(
            policy_id=policy_id,
            mode=ReviewMode(mode),
            scope_kind=ReviewScopeKind(scope_kind),
            scope_id=scope_id,
            scope_key=scope_key,
            enabled=enabled,
            conditions=conditions,
            reviewer=reviewer,
            fail_closed=fail_closed,
            priority=priority,
        )
    except (ValueError, TypeError):
        return None
