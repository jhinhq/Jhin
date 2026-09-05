"""Policy evaluator decision matrix (plan 12.2, 42; exit-test foundation)."""

import pytest
from pydantic import BaseModel, ValidationError

from jhin_policy import (
    ApprovalPreset,
    DecisionType,
    Grant,
    GrantEffect,
    PolicyRule,
    RiskLevel,
    RuleAction,
    ToolDefinition,
    authorizing_allow_grants,
    evaluate,
    matching_preset,
    result_scope_admits,
    rules_for_preset,
    scope_matches,
)


class _In(BaseModel):
    text: str = ""


class _Out(BaseModel):
    text: str = ""


def _tool(
    risk: RiskLevel,
    *,
    capability: str = "system.demo",
    supports_approval: bool = True,
    scope_keys: tuple[str, ...] = (),
    required_grant_scope_keys: tuple[str, ...] = (),
    result_scope_keys: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        name=capability,
        description="",
        risk=risk,
        input_model=_In,
        output_model=_Out,
        required_capability=capability,
        supports_approval=supports_approval,
        scope_keys=scope_keys,
        required_grant_scope_keys=required_grant_scope_keys,
        result_scope_keys=result_scope_keys,
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

    def test_required_grant_scope_rejects_unscoped_exact_and_wildcard_allows(self) -> None:
        tool = _tool(
            RiskLevel.READ,
            scope_keys=("connection_id", "schema"),
            required_grant_scope_keys=("connection_id", "schema"),
        )
        requested = {"connection_id": "connection-1", "schema": "public"}

        for capability in ("system.demo", "system.*", "*"):
            decision = evaluate(
                tool,
                grants=[Grant(capability=capability, effect=GrantEffect.ALLOW)],
                rules=[],
                requested_scope=requested,
            )
            assert decision.decision is DecisionType.DENY
            assert decision.code == "required_scope_missing"

    def test_required_grant_scope_accepts_only_a_fully_scoped_allow(self) -> None:
        tool = _tool(
            RiskLevel.READ,
            scope_keys=("connection_id", "schema"),
            required_grant_scope_keys=("connection_id", "schema"),
        )
        requested = {"connection_id": "connection-1", "schema": "public"}
        partial = Grant(
            capability="system.*",
            scope={"connection_id": "connection-1"},
            effect=GrantEffect.ALLOW,
        )
        complete = Grant(
            capability="system.*",
            scope={"connection_id": "connection-1", "schema": "public"},
            effect=GrantEffect.ALLOW,
        )

        assert (
            evaluate(tool, grants=[partial], rules=[], requested_scope=requested).code
            == "required_scope_missing"
        )
        assert evaluate(
            tool, grants=[partial, complete], rules=[], requested_scope=requested
        ).allowed

    def test_required_grant_scope_value_mismatch_is_scope_mismatch(self) -> None:
        tool = _tool(
            RiskLevel.READ,
            scope_keys=("connection_id", "schema"),
            required_grant_scope_keys=("connection_id", "schema"),
        )
        decision = evaluate(
            tool,
            grants=[
                Grant(
                    capability="system.demo",
                    scope={"connection_id": "connection-1", "schema": "private"},
                )
            ],
            rules=[],
            requested_scope={"connection_id": "connection-1", "schema": "public"},
        )

        assert decision.code == "scope_mismatch"

    def test_missing_required_requested_scope_has_a_distinct_denial_code(self) -> None:
        tool = _tool(
            RiskLevel.READ,
            scope_keys=("connection_id", "schema"),
            required_grant_scope_keys=("connection_id", "schema"),
        )
        decision = evaluate(
            tool,
            grants=[
                Grant(
                    capability="system.demo",
                    scope={"connection_id": "connection-1", "schema": "public"},
                )
            ],
            rules=[],
            requested_scope={"connection_id": "connection-1"},
        )
        assert decision.code == "required_scope_missing"

    def test_required_grant_scope_does_not_weaken_explicit_deny(self) -> None:
        tool = _tool(
            RiskLevel.READ,
            scope_keys=("connection_id",),
            required_grant_scope_keys=("connection_id",),
        )
        decision = evaluate(
            tool,
            grants=[
                Grant(
                    capability="system.demo",
                    scope={"connection_id": "connection-1"},
                    effect=GrantEffect.ALLOW,
                ),
                Grant(capability="system.*", effect=GrantEffect.DENY),
            ],
            rules=[],
            requested_scope={"connection_id": "connection-1"},
        )
        assert decision.code == "explicit_deny"

    @pytest.mark.parametrize(
        ("scope_keys", "required_scope_keys"),
        [
            (("connection_id",), ("schema",)),
            (("connection_id",), ("connection_id", "connection_id")),
            (("connection_id",), ("",)),
            (("connection_id",), ("bad key",)),
        ],
    )
    def test_tool_definition_rejects_invalid_required_scope_contract(
        self,
        scope_keys: tuple[str, ...],
        required_scope_keys: tuple[str, ...],
    ) -> None:
        with pytest.raises(ValidationError, match="required grant scope"):
            _tool(
                RiskLevel.READ,
                scope_keys=scope_keys,
                required_grant_scope_keys=required_scope_keys,
            )

    def test_deferred_scope_cannot_also_require_generic_grant_dimensions(self) -> None:
        with pytest.raises(ValidationError, match="defers_scope"):
            ToolDefinition(
                name="system.deferred_demo",
                description="",
                risk=RiskLevel.READ,
                input_model=_In,
                output_model=_Out,
                required_capability="system.deferred_demo",
                scope_keys=("relationship",),
                required_grant_scope_keys=("relationship",),
                defers_scope=True,
            )


class TestResultScopes:
    """A listing names no repository, so the grant's repository patterns
    bound its rows instead of its request (``result_scope_keys``)."""

    LISTING = _tool(
        RiskLevel.READ,
        scope_keys=("connection_id", "repository"),
        result_scope_keys=("repository",),
    )
    SCOPED = Grant(
        capability="system.demo",
        scope={"connection_id": "connection-1", "repository": "octo/*"},
        effect=GrantEffect.ALLOW,
    )

    def test_a_repository_scoped_grant_still_authorizes_a_listing(self) -> None:
        """The same grant that denies an out-of-scope *read* allows the
        listing: the dimension it constrains is one the call never names."""
        ordinary = _tool(RiskLevel.READ, scope_keys=("connection_id", "repository"))
        assert (
            evaluate(
                ordinary,
                grants=[self.SCOPED],
                rules=[],
                requested_scope={"connection_id": "connection-1"},
            ).code
            == "scope_mismatch"
        )
        assert evaluate(
            self.LISTING,
            grants=[self.SCOPED],
            rules=[],
            requested_scope={"connection_id": "connection-1"},
        ).allowed

    def test_the_other_dimensions_still_have_to_match(self) -> None:
        decision = evaluate(
            self.LISTING,
            grants=[self.SCOPED],
            rules=[],
            requested_scope={"connection_id": "connection-2"},
        )
        assert decision.code == "scope_mismatch"

    def test_a_repository_scoped_deny_blocks_the_whole_listing(self) -> None:
        """Fail closed: a listing cannot honour a deny row by row, so a deny
        naming a repository covers the call rather than leaking the name."""
        decision = evaluate(
            self.LISTING,
            grants=[
                self.SCOPED,
                Grant(
                    capability="system.demo",
                    scope={"repository": "octo/secret"},
                    effect=GrantEffect.DENY,
                ),
            ],
            rules=[],
            requested_scope={"connection_id": "connection-1"},
        )
        assert decision.code == "explicit_deny"

    def test_authorizing_grants_are_the_ones_the_decision_rested_on(self) -> None:
        other_connection = Grant(
            capability="system.demo",
            scope={"connection_id": "connection-2", "repository": "other/*"},
            effect=GrantEffect.ALLOW,
        )
        wrong_capability = Grant(capability="system.other", scope={}, effect=GrantEffect.ALLOW)
        a_deny = Grant(capability="system.demo", scope={}, effect=GrantEffect.DENY)
        assert authorizing_allow_grants(
            self.LISTING,
            grants=[self.SCOPED, other_connection, wrong_capability, a_deny],
            requested_scope={"connection_id": "connection-1"},
        ) == (self.SCOPED,)

    def test_rows_are_admitted_by_the_grant_patterns_not_the_request(self) -> None:
        assert result_scope_admits([self.SCOPED], "repository", "octo/widgets")
        assert not result_scope_admits([self.SCOPED], "repository", "other/widgets")

    def test_a_grant_leaving_the_dimension_open_admits_every_row(self) -> None:
        unscoped = Grant(capability="system.demo", scope={"connection_id": "connection-1"})
        assert result_scope_admits([unscoped], "repository", "anything/at-all")

    def test_no_authorizing_grants_admits_nothing(self) -> None:
        """An executor that lost its provenance returns an empty page."""
        assert not result_scope_admits([], "repository", "octo/widgets")

    def test_a_result_dimension_cannot_also_be_required_of_the_grant(self) -> None:
        with pytest.raises(ValidationError, match="both required of the grant"):
            _tool(
                RiskLevel.READ,
                scope_keys=("connection_id", "repository"),
                required_grant_scope_keys=("repository",),
                result_scope_keys=("repository",),
            )

    def test_a_result_dimension_must_be_a_scope_key(self) -> None:
        with pytest.raises(ValidationError, match="result scope keys"):
            _tool(
                RiskLevel.READ,
                scope_keys=("connection_id",),
                result_scope_keys=("repository",),
            )


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


def test_a_result_scope_admits_repositories_a_segment_at_a_time() -> None:
    """``result_scope_admits`` decides what an agent may see in a listing,
    so ``repository`` is matched by the repository matcher rather than by
    bare ``fnmatch``, whose ``*`` crosses ``/``. Other dimensions keep the
    evaluator's ordinary matching.
    """
    from jhin_policy import Grant, GrantEffect, result_scope_admits

    def grant(**scope: str) -> Grant:
        return Grant(
            capability="github.repository.list", scope=dict(scope), effect=GrantEffect.ALLOW
        )

    owner = [grant(repository="octo/*")]
    assert result_scope_admits(owner, "repository", "octo/alpha") is True
    # The trap: fnmatch would call this a match.
    assert result_scope_admits([grant(repository="octo*")], "repository", "octo-labs/x") is False
    assert result_scope_admits(owner, "repository", "octo-labs/x") is False
    # A value that is not a plain owner/name is never admitted, even by *.
    assert result_scope_admits([grant(repository="*")], "repository", "../evil") is False
    assert result_scope_admits([grant(repository="*")], "repository", "octo/alpha") is True
    # An unconstrained grant is unlimited, as everywhere else.
    assert result_scope_admits([grant(connection_id="c1")], "repository", "octo/alpha") is True
    # A dimension that is not a repository keeps ordinary matching.
    assert result_scope_admits([grant(branch="agent/*")], "branch", "agent/fix") is True
