"""Policy evaluator decision matrix (plan 12.2, 42; exit-test foundation)."""

from pydantic import BaseModel

from jhin_policy import (
    ApprovalPreset,
    DecisionType,
    Grant,
    GrantEffect,
    PolicyRule,
    RiskLevel,
    RuleAction,
    ToolDefinition,
    evaluate,
    matching_preset,
    rules_for_preset,
    scope_matches,
)


class _In(BaseModel):
    text: str = ""


class _Out(BaseModel):
    text: str = ""


def _tool(
    risk: RiskLevel, *, capability: str = "system.demo", supports_approval: bool = True
) -> ToolDefinition:
    return ToolDefinition(
        name=capability,
        description="",
        risk=risk,
        input_model=_In,
        output_model=_Out,
        required_capability=capability,
        supports_approval=supports_approval,
    )


ALLOW = Grant(capability="system.demo", effect=GrantEffect.ALLOW)


class TestGrants:
    def test_deny_by_default_without_any_grant(self) -> None:
        decision = evaluate(_tool(RiskLevel.READ), grants=[], rules=[])
        assert decision.decision is DecisionType.DENY
        assert decision.code == "no_grant"

    def test_granted_read_allows_automatically(self) -> None:
        decision = evaluate(_tool(RiskLevel.READ), grants=[ALLOW], rules=[])
        assert decision.decision is DecisionType.ALLOW

    def test_unrelated_grant_does_not_allow(self) -> None:
        grants = [Grant(capability="system.other", effect=GrantEffect.ALLOW)]
        decision = evaluate(_tool(RiskLevel.READ), grants=grants, rules=[])
        assert decision.code == "no_grant"

    def test_subtree_grant_allows(self) -> None:
        grants = [Grant(capability="system.*", effect=GrantEffect.ALLOW)]
        assert evaluate(_tool(RiskLevel.READ), grants=grants, rules=[]).allowed

    def test_explicit_deny_beats_allow(self) -> None:
        grants = [
            ALLOW,
            Grant(capability="system.demo", effect=GrantEffect.DENY),
        ]
        decision = evaluate(_tool(RiskLevel.READ), grants=grants, rules=[])
        assert decision.decision is DecisionType.DENY
        assert decision.code == "explicit_deny"

    def test_subtree_deny_beats_exact_allow(self) -> None:
        grants = [ALLOW, Grant(capability="system.*", effect=GrantEffect.DENY)]
        decision = evaluate(_tool(RiskLevel.READ), grants=grants, rules=[])
        assert decision.code == "explicit_deny"


class TestScopes:
    def test_empty_grant_scope_matches_anything(self) -> None:
        assert scope_matches({}, {})
        assert scope_matches({}, {"repository": "acme/api"})

    def test_missing_requested_key_fails(self) -> None:
        assert not scope_matches({"repository": "acme/api"}, {})

    def test_exact_value(self) -> None:
        assert scope_matches({"repository": "acme/api"}, {"repository": "acme/api"})
        assert not scope_matches({"repository": "acme/api"}, {"repository": "acme/web"})

    def test_list_means_any_of(self) -> None:
        granted = {"repository": ["acme/api", "acme/web"]}
        assert scope_matches(granted, {"repository": "acme/web"})
        assert not scope_matches(granted, {"repository": "acme/infra"})

    def test_wildcard_string_values(self) -> None:
        assert scope_matches({"branch": "agent/*"}, {"branch": "agent/fix-login"})
        assert not scope_matches({"branch": "agent/*"}, {"branch": "main"})

    def test_scoped_grant_denies_out_of_scope_call(self) -> None:
        grants = [
            Grant(
                capability="system.demo",
                scope={"repository": "acme/api"},
                effect=GrantEffect.ALLOW,
            )
        ]
        in_scope = evaluate(
            _tool(RiskLevel.READ),
            grants=grants,
            rules=[],
            requested_scope={"repository": "acme/api"},
        )
        out_of_scope = evaluate(
            _tool(RiskLevel.READ),
            grants=grants,
            rules=[],
            requested_scope={"repository": "acme/web"},
        )
        assert in_scope.allowed
        assert out_of_scope.code == "scope_mismatch"

    def test_scoped_deny_only_denies_within_scope(self) -> None:
        grants = [
            ALLOW,
            Grant(
                capability="system.demo",
                scope={"repository": "acme/api"},
                effect=GrantEffect.DENY,
            ),
        ]
        denied = evaluate(
            _tool(RiskLevel.READ),
            grants=grants,
            rules=[],
            requested_scope={"repository": "acme/api"},
        )
        allowed = evaluate(
            _tool(RiskLevel.READ),
            grants=grants,
            rules=[],
            requested_scope={"repository": "acme/web"},
        )
        assert denied.code == "explicit_deny"
        assert allowed.allowed


class TestRiskDefaults:
    """Plan 12.2: read/write auto once granted; elevated/destructive approval."""

    def test_read_write_auto(self) -> None:
        for risk in (RiskLevel.READ, RiskLevel.WRITE):
            assert evaluate(_tool(risk), grants=[ALLOW], rules=[]).allowed

    def test_elevated_destructive_require_approval(self) -> None:
        for risk in (RiskLevel.ELEVATED, RiskLevel.DESTRUCTIVE):
            decision = evaluate(_tool(risk), grants=[ALLOW], rules=[])
            assert decision.decision is DecisionType.REQUIRE_APPROVAL

    def test_approval_needed_but_unsupported_is_denied(self) -> None:
        tool = _tool(RiskLevel.DESTRUCTIVE, supports_approval=False)
        decision = evaluate(tool, grants=[ALLOW], rules=[])
        assert decision.decision is DecisionType.DENY
        assert decision.code == "approval_unsupported"


class TestRules:
    def test_rule_overrides_default(self) -> None:
        rules = [PolicyRule(risk=RiskLevel.ELEVATED, action=RuleAction.AUTO)]
        assert evaluate(_tool(RiskLevel.ELEVATED), grants=[ALLOW], rules=rules).allowed

    def test_forbid_rule_denies_despite_grant(self) -> None:
        rules = [PolicyRule(risk=RiskLevel.DESTRUCTIVE, action=RuleAction.FORBID)]
        decision = evaluate(_tool(RiskLevel.DESTRUCTIVE), grants=[ALLOW], rules=rules)
        assert decision.decision is DecisionType.DENY
        assert decision.code == "forbidden_by_policy"

    def test_write_approval_rule(self) -> None:
        rules = [PolicyRule(risk=RiskLevel.WRITE, action=RuleAction.APPROVAL)]
        decision = evaluate(_tool(RiskLevel.WRITE), grants=[ALLOW], rules=rules)
        assert decision.decision is DecisionType.REQUIRE_APPROVAL

    def test_capability_specific_rule_wins_by_order(self) -> None:
        """First match wins: a capability rule listed before the risk-wide
        rule takes precedence (plan 42 per-capability customization)."""
        rules = [
            PolicyRule(capability="system.demo", action=RuleAction.APPROVAL),
            PolicyRule(risk=RiskLevel.READ, action=RuleAction.AUTO),
        ]
        decision = evaluate(_tool(RiskLevel.READ), grants=[ALLOW], rules=rules)
        assert decision.decision is DecisionType.REQUIRE_APPROVAL

    def test_non_matching_rule_falls_through_to_default(self) -> None:
        rules = [PolicyRule(capability="github.*", action=RuleAction.FORBID)]
        assert evaluate(_tool(RiskLevel.READ), grants=[ALLOW], rules=rules).allowed


class TestPresets:
    def test_presets_round_trip(self) -> None:
        for preset in ApprovalPreset:
            assert matching_preset(rules_for_preset(preset)) is preset

    def test_custom_rules_match_no_preset(self) -> None:
        assert matching_preset([PolicyRule(action=RuleAction.AUTO)]) is None

    def test_autonomous_still_gates_destructive(self) -> None:
        rules = rules_for_preset(ApprovalPreset.AUTONOMOUS)
        elevated = evaluate(_tool(RiskLevel.ELEVATED), grants=[ALLOW], rules=rules)
        destructive = evaluate(_tool(RiskLevel.DESTRUCTIVE), grants=[ALLOW], rules=rules)
        assert elevated.allowed
        assert destructive.decision is DecisionType.REQUIRE_APPROVAL

    def test_balanced_gates_elevated_and_destructive(self) -> None:
        rules = rules_for_preset(ApprovalPreset.BALANCED)
        assert evaluate(_tool(RiskLevel.WRITE), grants=[ALLOW], rules=rules).allowed
        for risk in (RiskLevel.ELEVATED, RiskLevel.DESTRUCTIVE):
            decision = evaluate(_tool(risk), grants=[ALLOW], rules=rules)
            assert decision.decision is DecisionType.REQUIRE_APPROVAL

    def test_restricted_forbids_destructive_and_gates_write(self) -> None:
        rules = rules_for_preset(ApprovalPreset.RESTRICTED)
        assert evaluate(_tool(RiskLevel.READ), grants=[ALLOW], rules=rules).allowed
        write = evaluate(_tool(RiskLevel.WRITE), grants=[ALLOW], rules=rules)
        destructive = evaluate(_tool(RiskLevel.DESTRUCTIVE), grants=[ALLOW], rules=rules)
        assert write.decision is DecisionType.REQUIRE_APPROVAL
        assert destructive.code == "forbidden_by_policy"

    def test_grants_still_required_under_any_preset(self) -> None:
        """Presets never substitute for grants: deny-by-default holds."""
        for preset in ApprovalPreset:
            decision = evaluate(
                _tool(RiskLevel.READ), grants=[], rules=list(rules_for_preset(preset))
            )
            assert decision.code == "no_grant"
