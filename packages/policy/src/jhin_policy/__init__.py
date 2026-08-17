"""jhin-policy: capability registry, risk levels, and the pure policy
evaluator (plan sections 12 and 42).

This package performs no I/O. The tool gateway (jhin-tools) loads grants and
rules from the database and calls :func:`evaluate`.
"""

from jhin_policy.approvals import (
    PRESET_RULES,
    ApprovalPreset,
    matching_preset,
    rules_for_preset,
)
from jhin_policy.capabilities import (
    FORBIDDEN_CAPABILITY_PREFIXES,
    CapabilityRegistry,
    RegistryError,
    ToolDefinition,
    capability_matches,
    is_forbidden_capability,
    is_valid_capability,
)
from jhin_policy.delegation import (
    DEFAULT_MAX_TASK_DEPTH,
    DELEGATE_CAPABILITY,
    DelegationDecision,
    DelegationFacts,
    DelegationSettings,
    delegation_settings,
    evaluate_delegation,
)
from jhin_policy.evaluator import (
    DecisionType,
    Grant,
    GrantEffect,
    PolicyDecision,
    PolicyRule,
    evaluate,
    scope_matches,
)
from jhin_policy.risk import DEFAULT_ACTION_BY_RISK, RiskLevel, RuleAction

__all__ = [
    "DEFAULT_ACTION_BY_RISK",
    "DEFAULT_MAX_TASK_DEPTH",
    "DELEGATE_CAPABILITY",
    "FORBIDDEN_CAPABILITY_PREFIXES",
    "PRESET_RULES",
    "ApprovalPreset",
    "CapabilityRegistry",
    "DecisionType",
    "DelegationDecision",
    "DelegationFacts",
    "DelegationSettings",
    "Grant",
    "GrantEffect",
    "PolicyDecision",
    "PolicyRule",
    "RegistryError",
    "RiskLevel",
    "RuleAction",
    "ToolDefinition",
    "capability_matches",
    "delegation_settings",
    "evaluate",
    "evaluate_delegation",
    "is_forbidden_capability",
    "is_valid_capability",
    "matching_preset",
    "rules_for_preset",
    "scope_matches",
]
