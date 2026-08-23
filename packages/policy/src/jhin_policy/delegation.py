"""The delegation permission model (plan 7.5, 45 Phase 8).

Deny-by-default, expressed on capability-grant scopes:

- capability: ``organization.delegate`` — without an allow grant an agent
  cannot delegate at all (the gateway enforces this before execution).
- grant scope key ``targets`` — which *relationships* the agent may delegate
  across: ``"subordinates"`` (direct/indirect reports via the manager
  chain), ``"team"`` (agents sharing the delegator's team), ``"any"``
  (any active agent in the workspace), or a list of these.
  **Missing means ``"subordinates"``** — the plan's default policy.
- grant scope key ``target_agent_id`` — optional additional pin: an fnmatch
  pattern or list of agent UUID strings; both constraints must pass.

Independent structural guards (never overridable by grants):

- **no cycles**: the target agent must not already own a task on the
  delegating task's active ancestor chain — that lineage would deadlock a
  blocking wait and ping-pong a non-blocking one;
- **depth limit**: the child task's depth (ancestors + 1) must stay within
  the workspace limit (``settings_json.delegation.max_task_depth``,
  default 5);
- target must exist, be active, and live in the same workspace.

This module is pure (no I/O): callers load :class:`DelegationFacts` from
Postgres and receive a decision. Budget enforcement is a stub until the
plan-15.5 budget engine lands.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jhin_policy.capabilities import capability_matches
from jhin_policy.evaluator import Grant, GrantEffect, _scope_value_matches

DELEGATE_CAPABILITY = "organization.delegate"

DEFAULT_MAX_TASK_DEPTH = 5

_VALID_TARGETS = ("subordinates", "team", "any")


class DelegationFacts(BaseModel):
    """Everything the decision needs, resolved from Postgres by the caller."""

    model_config = ConfigDict(frozen=True)

    delegator_agent_id: str
    target_agent_id: str
    target_exists: bool = False
    target_active: bool = False
    # Relationship between delegator and target (computed via org graph).
    target_is_subordinate: bool = False
    target_in_same_team: bool = False
    # Depth of the *delegating* task (0 = root task); the child would sit at
    # task_depth + 1.
    task_depth: int = 0
    # Assigned agents of the delegating task and its active ancestors —
    # the lineage the cycle guard protects.
    ancestor_agent_ids: tuple[str, ...] = ()


class DelegationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    code: str
    reason: str


def _deny(code: str, reason: str) -> DelegationDecision:
    return DelegationDecision(allowed=False, code=code, reason=reason)


def _targets_of(scope: dict[str, Any]) -> tuple[str, ...]:
    """Normalize the ``targets`` scope value; missing = subordinates only."""
    raw = scope.get("targets", "subordinates")
    values = raw if isinstance(raw, list) else [raw]
    cleaned = tuple(str(v) for v in values if str(v) in _VALID_TARGETS)
    return cleaned or ("subordinates",)


def _relationship_permits(scope: dict[str, Any], facts: DelegationFacts) -> bool:
    for target in _targets_of(scope):
        if target == "any":
            return True
        if target == "subordinates" and facts.target_is_subordinate:
            return True
        if target == "team" and facts.target_in_same_team:
            return True
    return False


def _id_pin_permits(scope: dict[str, Any], facts: DelegationFacts) -> bool:
    if "target_agent_id" not in scope:
        return True
    return _scope_value_matches(scope["target_agent_id"], facts.target_agent_id)


def _grant_covers(grant: Grant, facts: DelegationFacts) -> bool:
    return _relationship_permits(grant.scope, facts) and _id_pin_permits(grant.scope, facts)


def evaluate_delegation(
    grants: Sequence[Grant],
    facts: DelegationFacts,
    *,
    max_task_depth: int = DEFAULT_MAX_TASK_DEPTH,
) -> DelegationDecision:
    """Decide one delegation request. Model output is never authorization
    (plan 52): only persisted grants and resolved org facts enter here."""
    if not facts.target_exists:
        return _deny("target_not_found", "target agent does not exist in this workspace")
    if not facts.target_active:
        return _deny("target_inactive", "target agent is not active")

    # Structural guards come before grant evaluation: no grant can waive them.
    if facts.target_agent_id in facts.ancestor_agent_ids:
        return _deny(
            "delegation_cycle",
            "target agent already owns a task on this lineage; delegating back would deadlock",
        )
    if facts.task_depth + 1 > max_task_depth:
        return _deny(
            "delegation_depth_exceeded",
            f"delegation chain depth limit is {max_task_depth}",
        )

    matching = [g for g in grants if capability_matches(g.capability, DELEGATE_CAPABILITY)]

    # Explicit deny beats allow, within the deny grant's own target scope.
    for grant in matching:
        if grant.effect is GrantEffect.DENY and _grant_covers(grant, facts):
            return _deny(
                "explicit_deny", "delegation to this target is explicitly denied for this agent"
            )

    allow = [g for g in matching if g.effect is GrantEffect.ALLOW]
    if not allow:
        return _deny("no_grant", f"agent has no capability grant matching '{DELEGATE_CAPABILITY}'")
    if not any(_grant_covers(grant, facts) for grant in allow):
        return _deny(
            "delegation_target_not_permitted",
            "no delegation grant permits this target (relationship or agent pin mismatch)",
        )

    # Budget (plan 7.5 "budget permits work"): monthly model budgets are
    # enforced at the run seams (jhin_db.budget — admission and each
    # reasoning step), so a delegated child's run is already bounded there.
    # No separate per-delegation spend cap exists; grants + structural
    # guards are the delegation-time controls.
    return DelegationDecision(
        allowed=True, code="granted", reason="delegation permitted by capability grant"
    )


class DelegationSettings(BaseModel):
    """Workspace-level delegation knobs (``workspace.settings_json.delegation``)."""

    model_config = ConfigDict(frozen=True)

    max_task_depth: int = Field(default=DEFAULT_MAX_TASK_DEPTH, ge=1, le=20)


def delegation_settings(settings_json: dict[str, Any] | None) -> DelegationSettings:
    """Parse workspace settings defensively; malformed values fall back."""
    raw = (settings_json or {}).get("delegation")
    if not isinstance(raw, dict):
        return DelegationSettings()
    try:
        return DelegationSettings.model_validate(raw)
    except Exception:
        return DelegationSettings()
