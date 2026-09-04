"""The approval posture for pushing code, pinned (plan 12.2, 42).

Two facts hold up the whole "a human sees the first thing that leaves the
sandbox" claim, and each is one edit away from being untrue, so each gets a
test that fails loudly rather than a comment:

1. ELEVATED requires approval under ``balanced`` — the preset the agent wizard
   selects by default (``apps/web/lib/wizard.ts``) — and under the bare risk
   defaults an agent has before any policy is set.
2. ELEVATED is *AUTO* under ``autonomous``. That is deliberate, and it is
   exactly why the Code-editing preset ships an explicit per-capability rule
   for ``cli.repository.push``: a capability-matched rule is found before a
   risk-matched one, so the gate survives an operator picking Autonomous.
"""

from pydantic import BaseModel

from jhin_policy import (
    ApprovalPreset,
    DecisionType,
    Grant,
    PolicyRule,
    RiskLevel,
    RuleAction,
    ToolDefinition,
    capability_rules,
    evaluate,
    matching_preset,
    rules_for_preset,
)
from jhin_policy.risk import DEFAULT_ACTION_BY_RISK


class _In(BaseModel):
    text: str = ""


class _Out(BaseModel):
    text: str = ""


PUSH = ToolDefinition(
    name="cli.repository.push",
    description="",
    risk=RiskLevel.ELEVATED,
    input_model=_In,
    output_model=_Out,
    required_capability="cli.repository.push",
    supports_approval=True,
)
GRANT = Grant(capability="cli.repository.push", scope={})
# What apps/web/lib/wizard.ts writes with the Code-editing preset's grants.
CODE_EDITING_RULE = PolicyRule(capability="cli.repository.push", action=RuleAction.APPROVAL)


def test_elevated_requires_approval_under_balanced() -> None:
    decision = evaluate(PUSH, grants=[GRANT], rules=list(rules_for_preset(ApprovalPreset.BALANCED)))
    assert decision.decision is DecisionType.REQUIRE_APPROVAL


def test_elevated_requires_approval_under_restricted() -> None:
    decision = evaluate(
        PUSH, grants=[GRANT], rules=list(rules_for_preset(ApprovalPreset.RESTRICTED))
    )
    assert decision.decision is DecisionType.REQUIRE_APPROVAL


def test_elevated_requires_approval_with_no_rules_at_all() -> None:
    """A freshly created agent has an empty approval_policy_json."""
    assert evaluate(PUSH, grants=[GRANT], rules=[]).decision is DecisionType.REQUIRE_APPROVAL


def test_autonomous_alone_does_not_gate_a_push() -> None:
    """Stated so nobody reads "push is ELEVATED" as "push always prompts"."""
    decision = evaluate(
        PUSH, grants=[GRANT], rules=list(rules_for_preset(ApprovalPreset.AUTONOMOUS))
    )
    assert decision.decision is DecisionType.ALLOW


def test_capability_rule_beats_risk_rule_for_push() -> None:
    """The Code-editing preset's explicit rule holds under Autonomous, which
    is the only reason "Autonomous still pauses for pushing code" is true."""
    rules = [CODE_EDITING_RULE, *rules_for_preset(ApprovalPreset.AUTONOMOUS)]
    decision = evaluate(PUSH, grants=[GRANT], rules=rules)
    assert decision.decision is DecisionType.REQUIRE_APPROVAL
    assert "policy rule" in decision.reason


def test_read_risk_auto_runs_under_every_preset_including_restricted() -> None:
    """Why a tool's declared risk is a security decision and not a label.

    READ is AUTO under all three presets, so anything declared READ runs with
    no human in the loop no matter how carefully an operator configures the
    agent. ``cli.test.run`` was READ while being an arbitrary shell inside a
    credentialed checkout; this is the property that made that matter.
    """
    reader = ToolDefinition(
        name="system.read_something",
        description="",
        risk=RiskLevel.READ,
        input_model=_In,
        output_model=_Out,
        required_capability="system.read_something",
    )
    grant = Grant(capability="system.read_something", scope={})
    for preset in ApprovalPreset:
        decision = evaluate(reader, grants=[grant], rules=list(rules_for_preset(preset)))
        assert decision.decision is DecisionType.ALLOW, preset


def test_a_write_risk_shell_is_still_unattended_under_balanced_but_seen_under_restricted() -> None:
    """What raising ``cli.test.run`` to WRITE buys and what it does not.

    It does not stop the attack — WRITE is AUTO under Autonomous and Balanced,
    which is deliberate, because a coding agent that stops for every test run
    is not a coding agent. Containment is structural: the push trusts nothing
    the command could have touched. What it does buy is that Restricted, which
    promises no unattended writes, now keeps that promise.
    """
    run = ToolDefinition(
        name="cli.test.run",
        description="",
        risk=RiskLevel.WRITE,
        input_model=_In,
        output_model=_Out,
        required_capability="cli.test.run",
        supports_approval=True,
    )
    grant = Grant(capability="cli.test.run", scope={})
    assert (
        evaluate(
            run, grants=[grant], rules=list(rules_for_preset(ApprovalPreset.BALANCED))
        ).decision
        is DecisionType.ALLOW
    )
    restricted = evaluate(
        run, grants=[grant], rules=list(rules_for_preset(ApprovalPreset.RESTRICTED))
    )
    assert restricted.decision is DecisionType.REQUIRE_APPROVAL


def test_the_capability_rule_gates_only_the_push() -> None:
    """The rule must not accidentally gate the reads and edits before it, or
    the agent stops being able to work uninterrupted."""
    checkout = ToolDefinition(
        name="cli.repository.checkout",
        description="",
        risk=RiskLevel.WRITE,
        input_model=_In,
        output_model=_Out,
        required_capability="cli.repository.checkout",
        supports_approval=True,
    )
    rules = [CODE_EDITING_RULE, *rules_for_preset(ApprovalPreset.BALANCED)]
    decision = evaluate(
        checkout,
        grants=[Grant(capability="cli.repository.checkout", scope={})],
        rules=rules,
    )
    assert decision.decision is DecisionType.ALLOW


def test_a_preset_speaks_for_risk_levels_and_nothing_else() -> None:
    """Which is why picking one can keep the rules it does not speak for."""
    for preset in ApprovalPreset:
        assert all(rule.capability == "*" for rule in rules_for_preset(preset))


def test_the_rules_a_preset_does_not_speak_for_are_the_capability_ones() -> None:
    rules = [CODE_EDITING_RULE, *rules_for_preset(ApprovalPreset.BALANCED)]
    assert capability_rules(rules) == (CODE_EDITING_RULE,)
    assert capability_rules(rules_for_preset(ApprovalPreset.BALANCED)) == ()


def test_the_preset_is_still_recognisable_underneath_a_capability_rule() -> None:
    """The display half of the same fix. While an extra rule made
    ``matching_preset`` answer None, the UI showed no mode selected at all —
    and the way to make a mode look selected was to click one, which is what
    used to replace the rules wholesale."""
    for preset in ApprovalPreset:
        assert matching_preset(rules_for_preset(preset)) is preset
        assert matching_preset([CODE_EDITING_RULE, *rules_for_preset(preset)]) is preset


def test_an_agent_with_no_risk_rules_reports_the_preset_it_actually_behaves_as() -> None:
    """The last way the UI could still show three unselected buttons.

    An agent whose only rule is a per-capability one — and a brand-new agent
    with no rules at all — has every other call answered by
    ``DEFAULT_ACTION_BY_RISK``. That is a mode, and it is Balanced's; reporting
    None said "no mode", which is the state that invites a click that replaces
    the rules the agent does have.
    """
    for rules in ([], [CODE_EDITING_RULE]):
        assert matching_preset(rules) is ApprovalPreset.BALANCED
    assert rules_for_preset(ApprovalPreset.BALANCED) == tuple(
        PolicyRule(capability="*", risk=risk, action=action)
        for risk, action in DEFAULT_ACTION_BY_RISK.items()
    ), "the reported preset must be the one the evaluator's own defaults expand to"


def test_rules_that_are_nobodys_preset_still_report_none() -> None:
    assert matching_preset([PolicyRule(action=RuleAction.AUTO)]) is None
    assert (
        matching_preset(
            [
                PolicyRule(capability="*", risk=RiskLevel.READ, action=RuleAction.AUTO),
                *rules_for_preset(ApprovalPreset.BALANCED),
            ]
        )
        is None
    )
