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
    CapabilityRegistry,
    RegistryError,
    ToolDefinition,
    capability_matches,
    is_forbidden_capability,
    is_valid_capability,
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
    "PRESET_RULES",
    "ApprovalPreset",
    "CapabilityRegistry",
    "DecisionType",
    "Grant",
    "GrantEffect",
    "PolicyDecision",
    "PolicyRule",
    "RegistryError",
    "RiskLevel",
    "RuleAction",
    "ToolDefinition",
    "capability_matches",
    "evaluate",
    "is_forbidden_capability",
    "is_valid_capability",
    "matching_preset",
    "rules_for_preset",
    "scope_matches",
]
