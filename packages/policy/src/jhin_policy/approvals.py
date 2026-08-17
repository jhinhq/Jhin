"""Approval-policy presets (plan 42).

Presets are a UI shortcut only: what is persisted on the agent is always the
explicit list of :class:`PolicyRule` rows the preset expands to. The
evaluator never sees preset names.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from jhin_policy.evaluator import PolicyRule
from jhin_policy.risk import RiskLevel, RuleAction


class ApprovalPreset(StrEnum):
    AUTONOMOUS = "autonomous"
    BALANCED = "balanced"
    RESTRICTED = "restricted"


def _rules(*pairs: tuple[RiskLevel, RuleAction]) -> tuple[PolicyRule, ...]:
    return tuple(PolicyRule(capability="*", risk=risk, action=action) for risk, action in pairs)


# Destructive actions keep a human in the loop even for Autonomous agents
# (plan 12.2: "destructive: human approval by default").
PRESET_RULES: dict[ApprovalPreset, tuple[PolicyRule, ...]] = {
    ApprovalPreset.AUTONOMOUS: _rules(
        (RiskLevel.READ, RuleAction.AUTO),
        (RiskLevel.WRITE, RuleAction.AUTO),
        (RiskLevel.ELEVATED, RuleAction.AUTO),
        (RiskLevel.DESTRUCTIVE, RuleAction.APPROVAL),
    ),
    ApprovalPreset.BALANCED: _rules(
        (RiskLevel.READ, RuleAction.AUTO),
        (RiskLevel.WRITE, RuleAction.AUTO),
        (RiskLevel.ELEVATED, RuleAction.APPROVAL),
        (RiskLevel.DESTRUCTIVE, RuleAction.APPROVAL),
    ),
    ApprovalPreset.RESTRICTED: _rules(
        (RiskLevel.READ, RuleAction.AUTO),
        (RiskLevel.WRITE, RuleAction.APPROVAL),
        (RiskLevel.ELEVATED, RuleAction.APPROVAL),
        (RiskLevel.DESTRUCTIVE, RuleAction.FORBID),
    ),
}


def rules_for_preset(preset: ApprovalPreset) -> tuple[PolicyRule, ...]:
    return PRESET_RULES[preset]


def matching_preset(rules: Sequence[PolicyRule]) -> ApprovalPreset | None:
    """The preset these exact rules correspond to, if any (UI display)."""
    as_tuple = tuple(rules)
    for preset, preset_rules in PRESET_RULES.items():
        if as_tuple == preset_rules:
            return preset
    return None
