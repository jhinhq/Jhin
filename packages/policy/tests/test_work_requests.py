"""Pure work-request decisions: deny-by-default, relationship scopes, explicit
deny, structural guards (self/inactive/unavailable/depth/caps/ping-pong)."""

from __future__ import annotations

from typing import Any

from jhin_policy import (
    DIRECTORY_READ_CAPABILITY,
    WORK_REQUEST_CAPABILITY,
    WORK_RESPOND_CAPABILITY,
    CoordinationSettings,
    Grant,
    GrantEffect,
    WorkRequestFacts,
    collaboration_grant_specs,
    coordination_settings,
    evaluate_work_request,
)

REQUESTER = "11111111-1111-1111-1111-111111111111"
TARGET = "22222222-2222-2222-2222-222222222222"


def facts(**overrides: Any) -> WorkRequestFacts:
    values: dict[str, Any] = {
        "requester_agent_id": REQUESTER,
        "target_agent_id": TARGET,
        "target_exists": True,
        "target_active": True,
        "target_available": True,
        "target_in_same_team": True,
    }
    values.update(overrides)
    return WorkRequestFacts(**values)


def allow(
    scope: dict[str, Any] | None = None, capability: str = "organization.work.request"
) -> Grant:
    return Grant(capability=capability, scope=scope or {}, effect=GrantEffect.ALLOW)


def deny(scope: dict[str, Any] | None = None) -> Grant:
    return Grant(capability="organization.work.request", scope=scope or {}, effect=GrantEffect.DENY)


def test_deny_by_default_without_grant() -> None:
    decision = evaluate_work_request([], facts())
    assert not decision.allowed
    assert decision.code == "no_grant"


def test_team_is_the_default_reach() -> None:
    assert evaluate_work_request([allow()], facts()).allowed
    cross_team = evaluate_work_request([allow()], facts(target_in_same_team=False))
    assert cross_team.code == "request_target_not_permitted"


def test_any_scope_reaches_other_teams_and_subtree_grant_matches() -> None:
    assert evaluate_work_request(
        [allow({"targets": "any"}, capability="organization.*")],
        facts(target_in_same_team=False),
    ).allowed


def test_subordinates_scope() -> None:
    grants = [allow({"targets": "subordinates"})]
    assert evaluate_work_request(grants, facts(target_is_subordinate=True)).allowed
    assert not evaluate_work_request(grants, facts(target_is_subordinate=False)).allowed


def test_explicit_deny_wins_within_its_scope() -> None:
    grants = [allow({"targets": "any"}), deny({"targets": "any", "target_agent_id": TARGET})]
    decision = evaluate_work_request(grants, facts(target_in_same_team=False))
    assert decision.code == "explicit_deny"
    other = facts(target_agent_id="33333333-3333-3333-3333-333333333333", target_in_same_team=False)
    assert evaluate_work_request(grants, other).allowed


def test_relationship_is_never_authority() -> None:
    # Manager/teammate facts without a grant still deny.
    decision = evaluate_work_request([], facts(target_is_subordinate=True))
    assert decision.code == "no_grant"


def test_structural_guards_precede_grants() -> None:
    grants = [allow({"targets": "any"})]
    assert evaluate_work_request(grants, facts(target_agent_id=REQUESTER)).code == "self_request"
    assert evaluate_work_request(grants, facts(target_exists=False)).code == "target_not_found"
    assert evaluate_work_request(grants, facts(target_active=False)).code == "target_inactive"
    assert evaluate_work_request(grants, facts(target_available=False)).code == "target_unavailable"
    assert evaluate_work_request(grants, facts(reverse_request_open=True)).code == (
        "request_ping_pong"
    )
    assert evaluate_work_request(grants, facts(request_depth=5)).code == ("request_depth_exceeded")
    assert evaluate_work_request(grants, facts(request_depth=4)).allowed


def test_caps_from_workspace_settings() -> None:
    grants = [allow({"targets": "any"})]
    settings = coordination_settings(
        {
            "coordination": {
                "max_request_depth": 2,
                "max_pending_requests_per_agent": 1,
                "max_requests_per_agent_per_hour": 2,
                "max_active_request_tasks_per_agent": 1,
            }
        }
    )
    assert settings.max_request_depth == 2
    assert evaluate_work_request(grants, facts(request_depth=3), settings).code == (
        "request_depth_exceeded"
    )
    assert evaluate_work_request(grants, facts(open_requests_by_requester=1), settings).code == (
        "requester_pending_limit"
    )
    assert evaluate_work_request(
        grants, facts(requests_last_hour_by_requester=2), settings
    ).code == ("requester_rate_limit")
    assert evaluate_work_request(
        grants, facts(active_request_tasks_for_target=1), settings
    ).code == ("target_capacity_exceeded")
    assert evaluate_work_request(grants, facts(), settings).allowed


def test_malformed_settings_fall_back_to_defaults() -> None:
    assert coordination_settings({"coordination": {"max_request_depth": 0}}) == (
        CoordinationSettings()
    )
    assert coordination_settings({"coordination": "nope"}) == CoordinationSettings()
    assert coordination_settings(None).max_request_depth == 4


def test_collaboration_baseline_is_safe_by_default() -> None:
    """The collaboration baseline grants directory read + ask + respond with
    a cross-team (targets: any) request scope, and never delegation."""
    specs = dict(collaboration_grant_specs())
    assert set(specs) == {
        DIRECTORY_READ_CAPABILITY,
        WORK_REQUEST_CAPABILITY,
        WORK_RESPOND_CAPABILITY,
    }
    assert specs[WORK_REQUEST_CAPABILITY] == {"targets": "any"}
    assert "organization.delegate" not in specs

    # The request grant it produces actually permits an ordinary cross-team
    # ask (neither subordinate nor same team) through the pure evaluator.
    grants = [Grant(capability=WORK_REQUEST_CAPABILITY, scope=specs[WORK_REQUEST_CAPABILITY])]
    cross_team = WorkRequestFacts(
        requester_agent_id=REQUESTER,
        target_agent_id=TARGET,
        target_exists=True,
        target_active=True,
        target_is_subordinate=False,
        target_in_same_team=False,
    )
    assert evaluate_work_request(grants, cross_team).allowed
