"""The pure policy decision function (plan 12).

``evaluate`` combines three inputs into one decision:

1. capability grants (plan 6.6) — deny-by-default, explicit deny beats allow,
   scopes must match;
2. the agent's persisted approval-policy rules (plan 42);
3. risk-level defaults (plan 12.2) when no rule matches.

It is deliberately free of I/O and of any model/tool execution concern so the
full decision matrix is unit-testable. Callers (the tool gateway) load grants
and rules from Postgres and pass them in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jhin_policy.capabilities import ToolDefinition, capability_matches
from jhin_policy.risk import DEFAULT_ACTION_BY_RISK, RiskLevel, RuleAction


class GrantEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Grant(BaseModel):
    """In-memory mirror of one agent_capability_grant row (plan 6.6)."""

    model_config = ConfigDict(frozen=True)

    capability: str
    scope: dict[str, Any] = Field(default_factory=dict)
    effect: GrantEffect = GrantEffect.ALLOW


class PolicyRule(BaseModel):
    """One persisted approval-policy rule (plan 42).

    Rules are evaluated first-match. Phase 4 presets emit risk-only rules
    (``capability="*"``); per-capability rules (e.g. require approval for
    ``github.pull_request.merge`` only) already work for Phase 5 connectors.
    """

    model_config = ConfigDict(frozen=True)

    capability: str = "*"
    risk: RiskLevel | None = None
    action: RuleAction


class DecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: DecisionType
    code: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is DecisionType.ALLOW


def _scope_value_matches(granted: Any, requested: Any) -> bool:
    """One grant scope value against one requested value.

    Strings support ``fnmatch``-style wildcards (``agent/*`` matches
    ``agent/fix-login``); lists mean "any of these"; anything else is strict
    equality.
    """
    if isinstance(granted, list):
        return any(_scope_value_matches(item, requested) for item in granted)
    if isinstance(granted, str) and isinstance(requested, str):
        return fnmatchcase(requested, granted)
    return bool(granted == requested)


def scope_matches(granted_scope: Mapping[str, Any], requested_scope: Mapping[str, Any]) -> bool:
    """Every key the grant constrains must be present and matching in the
    request. An empty grant scope matches any request (unscoped grant)."""
    for key, granted in granted_scope.items():
        if key not in requested_scope:
            return False
        if not _scope_value_matches(granted, requested_scope[key]):
            return False
    return True


def _first_matching_rule(
    rules: Sequence[PolicyRule], capability: str, risk: RiskLevel
) -> PolicyRule | None:
    for rule in rules:
        if not capability_matches(rule.capability, capability):
            continue
        if rule.risk is not None and rule.risk is not risk:
            continue
        return rule
    return None


def evaluate(
    tool: ToolDefinition,
    *,
    grants: Sequence[Grant],
    rules: Sequence[PolicyRule],
    requested_scope: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Decide one tool call: allow / deny(reason) / require_approval(reason).

    Model output is never authorization (plan 52): this function sees only
    the tool definition and the agent's persisted configuration.
    """
    capability = tool.required_capability
    scope = requested_scope or {}

    # 1. Explicit deny beats everything (within its scope).
    for grant in grants:
        if (
            grant.effect is GrantEffect.DENY
            and capability_matches(grant.capability, capability)
            and scope_matches(grant.scope, scope)
        ):
            return PolicyDecision(
                decision=DecisionType.DENY,
                code="explicit_deny",
                reason=f"capability '{capability}' is explicitly denied for this agent",
            )

    # 2. Deny-by-default: an allow grant must match capability *and* scope.
    allow_grants = [
        grant
        for grant in grants
        if grant.effect is GrantEffect.ALLOW and capability_matches(grant.capability, capability)
    ]
    if not allow_grants:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="no_grant",
            reason=f"agent has no capability grant matching '{capability}'",
        )
    if not any(scope_matches(grant.scope, scope) for grant in allow_grants):
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="scope_mismatch",
            reason=f"grant for '{capability}' does not cover the requested scope",
        )

    # 3. Approval-policy rules (first match), then risk defaults (plan 12.2).
    rule = _first_matching_rule(rules, capability, tool.risk)
    action = rule.action if rule is not None else DEFAULT_ACTION_BY_RISK[tool.risk]
    source = "policy rule" if rule is not None else f"default for {tool.risk.value} risk"

    if action is RuleAction.FORBID:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="forbidden_by_policy",
            reason=f"{tool.risk.value}-risk call forbidden by {source}",
        )
    if action is RuleAction.APPROVAL:
        if not tool.supports_approval:
            return PolicyDecision(
                decision=DecisionType.DENY,
                code="approval_unsupported",
                reason=(
                    f"{source} requires human approval but tool '{tool.name}' "
                    "does not support approval-gated execution"
                ),
            )
        return PolicyDecision(
            decision=DecisionType.REQUIRE_APPROVAL,
            code="approval_required",
            reason=f"{tool.risk.value}-risk call requires human approval ({source})",
        )
    return PolicyDecision(
        decision=DecisionType.ALLOW,
        code="granted",
        reason=f"capability granted; {source} permits automatic execution",
    )
