"""Delegation permission decision matrix (plan 7.5, 45 Phase 8)."""

from __future__ import annotations

from typing import Any

from jhin_policy import (
    DelegationFacts,
    DelegationSettings,
    Grant,
    GrantEffect,
    delegation_settings,
    evaluate_delegation,
)


def facts(**overrides: Any) -> DelegationFacts:
    values: dict[str, Any] = {
        "delegator_agent_id": "cto",
        "target_agent_id": "swe",
        "target_exists": True,
        "target_active": True,
        "target_is_subordinate": True,
        "target_in_same_team": True,
        "task_depth": 0,
        "ancestor_agent_ids": ("cto",),
    }
    values.update(overrides)
    return DelegationFacts(**values)


def allow(scope: dict[str, Any] | None = None) -> Grant:
    return Grant(capability="organization.delegate", scope=scope or {}, effect=GrantEffect.ALLOW)


# --- deny-by-default and grant matching ---


def test_no_grant_is_denied() -> None:
    decision = evaluate_delegation([], facts())
    assert not decision.allowed
    assert decision.code == "no_grant"


def test_unrelated_grants_do_not_permit_delegation() -> None:
    grants = [Grant(capability="github.pull_request.create", effect=GrantEffect.ALLOW)]
    assert evaluate_delegation(grants, facts()).code == "no_grant"


def test_wildcard_capability_grant_matches() -> None:
    grants = [Grant(capability="organization.*", effect=GrantEffect.ALLOW)]
    assert evaluate_delegation(grants, facts()).allowed


def test_explicit_deny_beats_allow() -> None:
    grants = [
        allow({"targets": "any"}),
        Grant(capability="organization.delegate", effect=GrantEffect.DENY),
    ]
    assert evaluate_delegation(grants, facts()).code == "explicit_deny"


def test_scoped_deny_only_hits_its_target() -> None:
    grants = [
        allow({"targets": "any"}),
        Grant(
            capability="organization.delegate",
            scope={"target_agent_id": "blogger"},
            effect=GrantEffect.DENY,
        ),
    ]
    assert evaluate_delegation(grants, facts(target_agent_id="blogger")).code == "explicit_deny"
    assert evaluate_delegation(grants, facts(target_agent_id="swe")).allowed


# --- relationship model (targets scope) ---


def test_default_scope_means_subordinates_only() -> None:
    grants = [allow()]
    assert evaluate_delegation(grants, facts(target_is_subordinate=True)).allowed
    denied = evaluate_delegation(
        grants, facts(target_is_subordinate=False, target_in_same_team=True)
    )
    assert denied.code == "delegation_target_not_permitted"


def test_team_scope_permits_same_team_but_not_strangers() -> None:
    grants = [allow({"targets": "team"})]
    ok = facts(target_is_subordinate=False, target_in_same_team=True)
    assert evaluate_delegation(grants, ok).allowed
    stranger = facts(target_is_subordinate=False, target_in_same_team=False)
    assert evaluate_delegation(grants, stranger).code == "delegation_target_not_permitted"


def test_any_scope_permits_unrelated_active_agents() -> None:
    grants = [allow({"targets": "any"})]
    stranger = facts(target_is_subordinate=False, target_in_same_team=False)
    assert evaluate_delegation(grants, stranger).allowed


def test_targets_list_is_a_union() -> None:
    grants = [allow({"targets": ["subordinates", "team"]})]
    assert evaluate_delegation(
        grants, facts(target_is_subordinate=False, target_in_same_team=True)
    ).allowed


def test_malformed_targets_value_falls_back_to_subordinates() -> None:
    grants = [allow({"targets": "everyone-please"})]
    assert evaluate_delegation(grants, facts(target_is_subordinate=True)).allowed
    assert not evaluate_delegation(grants, facts(target_is_subordinate=False)).allowed


def test_target_agent_id_pin_restricts_within_relationship() -> None:
    grants = [allow({"targets": "team", "target_agent_id": ["qa"]})]
    qa = facts(target_agent_id="qa", target_is_subordinate=False, target_in_same_team=True)
    assert evaluate_delegation(grants, qa).allowed
    other = facts(target_agent_id="swe", target_is_subordinate=False, target_in_same_team=True)
    assert evaluate_delegation(grants, other).code == "delegation_target_not_permitted"


# --- structural guards (not waivable by grants) ---


def test_missing_target_denied() -> None:
    decision = evaluate_delegation([allow({"targets": "any"})], facts(target_exists=False))
    assert decision.code == "target_not_found"


def test_inactive_target_denied() -> None:
    decision = evaluate_delegation([allow({"targets": "any"})], facts(target_active=False))
    assert decision.code == "target_inactive"


def test_cycle_guard_blocks_lineage_ping_pong() -> None:
    # SWE's task descends from a CTO task; delegating back to the CTO would
    # deadlock the blocking chain.
    decision = evaluate_delegation(
        [allow({"targets": "any"})],
        facts(
            delegator_agent_id="swe",
            target_agent_id="cto",
            ancestor_agent_ids=("cto", "swe"),
        ),
    )
    assert decision.code == "delegation_cycle"


def test_self_delegation_is_a_cycle() -> None:
    decision = evaluate_delegation(
        [allow({"targets": "any"})],
        facts(delegator_agent_id="swe", target_agent_id="swe", ancestor_agent_ids=("swe",)),
    )
    assert decision.code == "delegation_cycle"


def test_depth_limit_enforced_and_configurable() -> None:
    grants = [allow({"targets": "any"})]
    at_limit = facts(task_depth=4)
    assert evaluate_delegation(grants, at_limit).allowed  # child lands at depth 5
    over = facts(task_depth=5)
    assert evaluate_delegation(grants, over).code == "delegation_depth_exceeded"
    assert evaluate_delegation(grants, over, max_task_depth=6).allowed


# --- workspace settings parsing ---


def test_delegation_settings_defaults_and_fallbacks() -> None:
    assert delegation_settings(None) == DelegationSettings(max_task_depth=5)
    assert delegation_settings({}) == DelegationSettings(max_task_depth=5)
    assert delegation_settings({"delegation": {"max_task_depth": 3}}).max_task_depth == 3
    assert delegation_settings({"delegation": "junk"}).max_task_depth == 5
    assert delegation_settings({"delegation": {"max_task_depth": -2}}).max_task_depth == 5
