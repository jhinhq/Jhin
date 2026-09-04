"""Approval-policy presets (plan 42).

Presets are a UI shortcut only: what is persisted on the agent is always the
explicit list of :class:`PolicyRule` rows the preset expands to. The
evaluator never sees preset names.

A preset speaks for *risk levels* and nothing else — every rule it expands to
carries ``capability="*"``. A rule that names a capability is a separate
decision about one tool (the approval gate the Code-editing bundle keeps on
``cli.repository.push``, for instance), which is why :func:`capability_rules`
exists: picking a different preset re-states the risk levels and leaves those
decisions standing, and :func:`matching_preset` still recognises the preset
underneath them rather than reporting "no preset" and inviting a click that
would replace the lot.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from jhin_policy.evaluator import PolicyRule
from jhin_policy.risk import DEFAULT_ACTION_BY_RISK, RiskLevel, RuleAction


class ApprovalPreset(StrEnum):
    AUTONOMOUS = "autonomous"
    BALANCED = "balanced"
    RESTRICTED = "restricted"


ANY_CAPABILITY = "*"


def _rules(*pairs: tuple[RiskLevel, RuleAction]) -> tuple[PolicyRule, ...]:
    return tuple(
        PolicyRule(capability=ANY_CAPABILITY, risk=risk, action=action) for risk, action in pairs
    )


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


# What an agent with no risk-level rules of its own actually gets: the
# evaluator falls through to ``DEFAULT_ACTION_BY_RISK`` (plan 12.2). Written as
# rules so it can be compared against a preset expansion, and derived from the
# defaults rather than restated, so it follows them if they ever move.
_DEFAULT_RULES = _rules(*((risk, DEFAULT_ACTION_BY_RISK[risk]) for risk in RiskLevel))


def rules_for_preset(preset: ApprovalPreset) -> tuple[PolicyRule, ...]:
    return PRESET_RULES[preset]


def capability_rules(rules: Sequence[PolicyRule]) -> tuple[PolicyRule, ...]:
    """The rules a preset does not speak for: per-capability decisions, in the
    order they were written. Kept across a change of preset, and kept *first*,
    because rules are first-match and a risk-level rule ahead of one of these
    would answer for the capability before it was reached."""
    return tuple(rule for rule in rules if rule.capability != ANY_CAPABILITY)


def matching_preset(rules: Sequence[PolicyRule]) -> ApprovalPreset | None:
    """The preset these rules correspond to, if any (UI display).

    Compared against the risk-level rules alone. A per-capability rule beside
    them does not stop the preset being what it is, and reporting "no preset"
    because of one would put the UI in a state where every preset button looks
    unselected — an invitation to press one and find out.

    An agent with *no* risk-level rules is in that same position for a
    different reason: the evaluator answers its calls from the risk defaults,
    so it does have a mode, and the preset that expands to those defaults is
    the honest name for it. Answering None there was the last way the UI could
    still show three unselected buttons — for an agent whose only rule is a
    per-capability one, and for a brand-new agent with no rules at all.
    """
    risk_rules = tuple(rule for rule in rules if rule.capability == ANY_CAPABILITY)
    for preset, preset_rules in PRESET_RULES.items():
        if (risk_rules or _DEFAULT_RULES) == preset_rules:
            return preset
    return None
