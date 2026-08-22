"""Pure review-policy matching and deterministic reviewer resolution."""

from __future__ import annotations

from typing import Any

from jhin_domain import ReviewMode, ReviewScopeKind
from jhin_policy import (
    ReviewCondition,
    ReviewConditionKind,
    ReviewContext,
    ReviewerCandidates,
    ReviewerSelector,
    ReviewPolicySpec,
    evaluate_review_policies,
    policy_spec_from_row,
    resolve_reviewer,
)

AGENT = "aaaaaaaa-0000-0000-0000-000000000001"
MANAGER = "aaaaaaaa-0000-0000-0000-000000000002"
NAMED = "aaaaaaaa-0000-0000-0000-000000000003"
TEAM = "bbbbbbbb-0000-0000-0000-000000000001"


def policy(policy_id: str, *kinds: ReviewConditionKind, **overrides: Any) -> ReviewPolicySpec:
    values: dict[str, Any] = {
        "policy_id": policy_id,
        "mode": ReviewMode.PRE_ACTION,
        "conditions": tuple(ReviewCondition(kind=k) for k in kinds),
    }
    values.update(overrides)
    return ReviewPolicySpec(**values)


def context(**overrides: Any) -> ReviewContext:
    values: dict[str, Any] = {"mode": ReviewMode.PRE_ACTION, "agent_id": AGENT, "team_ids": (TEAM,)}
    values.update(overrides)
    return ReviewContext(**values)


def test_routine_work_proceeds_without_review() -> None:
    policies = [policy("p1", ReviewConditionKind.DESTRUCTIVE_ACTION)]
    decision = evaluate_review_policies(policies, context(risk="write"))
    assert not decision.required
    assert decision.code == "no_review"


def test_condition_matching_by_kind() -> None:
    cases = [
        (ReviewConditionKind.ELEVATED_ACTION, {"risk": "elevated"}),
        (ReviewConditionKind.DESTRUCTIVE_ACTION, {"risk": "destructive"}),
        (ReviewConditionKind.TOOL_FAILURE, {"tool_failed": True}),
        (ReviewConditionKind.TEST_FAILURE, {"test_failed": True}),
        (ReviewConditionKind.APPROVAL_DENIED, {"approval_denied": True}),
        (ReviewConditionKind.POLICY_DENIED, {"policy_denied": True}),
        (ReviewConditionKind.BLOCKED, {"blocked": True}),
        (ReviewConditionKind.CROSS_TEAM_REQUEST, {"cross_team_request": True}),
        (ReviewConditionKind.EXPLICIT_REQUEST, {"explicit_request": True}),
        (ReviewConditionKind.ALWAYS, {}),
    ]
    for kind, facts in cases:
        decision = evaluate_review_policies([policy("p", kind)], context(**facts))
        assert decision.required, kind
        assert decision.primary is not None
        assert decision.primary.matched_conditions == (kind,)
    # Elevated condition does not fire for plain writes.
    assert not evaluate_review_policies(
        [policy("p", ReviewConditionKind.ELEVATED_ACTION)], context(risk="write")
    ).required


def test_threshold_conditions() -> None:
    cost = ReviewPolicySpec(
        policy_id="cost",
        mode=ReviewMode.BEFORE_CLOSE,
        conditions=(ReviewCondition(kind=ReviewConditionKind.COST_THRESHOLD, threshold=1_000_000),),
    )
    assert not evaluate_review_policies(
        [cost], context(mode=ReviewMode.BEFORE_CLOSE, cost_micros=999_999)
    ).required
    assert evaluate_review_policies(
        [cost], context(mode=ReviewMode.BEFORE_CLOSE, cost_micros=1_000_000)
    ).required
    low = ReviewPolicySpec(
        policy_id="conf",
        mode=ReviewMode.BEFORE_CLOSE,
        conditions=(ReviewCondition(kind=ReviewConditionKind.LOW_CONFIDENCE, threshold=0.7),),
    )
    assert evaluate_review_policies(
        [low], context(mode=ReviewMode.BEFORE_CLOSE, confidence=0.5)
    ).required
    assert not evaluate_review_policies(
        [low], context(mode=ReviewMode.BEFORE_CLOSE, confidence=None)
    ).required
    # A threshold condition without a threshold never fires.
    missing = ReviewPolicySpec(
        policy_id="m",
        mode=ReviewMode.BEFORE_CLOSE,
        conditions=(ReviewCondition(kind=ReviewConditionKind.TOKEN_THRESHOLD),),
    )
    assert not evaluate_review_policies(
        [missing], context(mode=ReviewMode.BEFORE_CLOSE, total_tokens=10**9)
    ).required


def test_scope_mode_and_enabled_filters() -> None:
    always = ReviewConditionKind.ALWAYS
    other_agent = policy("agent", always, scope_kind=ReviewScopeKind.AGENT, scope_id=MANAGER)
    my_team = policy("team", always, scope_kind=ReviewScopeKind.TEAM, scope_id=TEAM)
    task_type = policy("tt", always, scope_kind=ReviewScopeKind.TASK_TYPE, scope_key="deploy")
    disabled = policy("off", always, enabled=False)
    post = policy("post", always, mode=ReviewMode.POST_ACTION)
    decision = evaluate_review_policies(
        [other_agent, my_team, task_type, disabled, post], context(task_type="chat")
    )
    assert [r.policy_id for r in decision.requirements] == ["team"]
    assert decision.blocking
    post_decision = evaluate_review_policies([post], context(mode=ReviewMode.POST_ACTION))
    assert post_decision.required and not post_decision.blocking


def test_most_specific_scope_then_priority_then_id() -> None:
    always = ReviewConditionKind.ALWAYS
    policies = [
        policy("ws-b", always, priority=10),
        policy("ws-a", always, priority=10),
        policy("team", always, scope_kind=ReviewScopeKind.TEAM, scope_id=TEAM, priority=500),
        policy("agent", always, scope_kind=ReviewScopeKind.AGENT, scope_id=AGENT, priority=900),
        policy("ws-first", always, priority=1),
    ]
    decision = evaluate_review_policies(policies, context())
    assert [r.policy_id for r in decision.requirements] == [
        "agent",
        "team",
        "ws-first",
        "ws-a",
        "ws-b",
    ]


def candidates(**overrides: Any) -> ReviewerCandidates:
    values: dict[str, Any] = {
        "manager_agent_id": MANAGER,
        "active_agents": {MANAGER: True, NAMED: True, AGENT: True},
        "team_role_agents": {"qa": (NAMED,)},
    }
    values.update(overrides)
    return ReviewerCandidates(**values)


def test_reporting_manager_resolves() -> None:
    resolution = resolve_reviewer(
        ReviewerSelector(), candidates(), mode=ReviewMode.PRE_ACTION, fail_closed=True
    )
    assert resolution.outcome == "resolved"
    assert resolution.reviewer_agent_id == MANAGER


def test_named_agent_team_role_and_human_selectors() -> None:
    named = resolve_reviewer(
        ReviewerSelector(kind="agent", agent_id=NAMED),
        candidates(),
        mode=ReviewMode.PRE_ACTION,
        fail_closed=False,
    )
    assert named.reviewer_agent_id == NAMED
    role = resolve_reviewer(
        ReviewerSelector(kind="team_role", role_label="qa"),
        candidates(),
        mode=ReviewMode.PRE_ACTION,
        fail_closed=False,
    )
    assert role.reviewer_agent_id == NAMED
    human = resolve_reviewer(
        ReviewerSelector(kind="human"), candidates(), mode=ReviewMode.PRE_ACTION, fail_closed=False
    )
    assert human.outcome == "human_required"


def test_managerless_fallback_chain() -> None:
    no_manager = candidates(manager_agent_id=None)
    # 1. named fallback agent
    res = resolve_reviewer(
        ReviewerSelector(fallback_agent_id=NAMED),
        no_manager,
        mode=ReviewMode.PRE_ACTION,
        fail_closed=True,
    )
    assert res.outcome == "resolved" and res.reviewer_agent_id == NAMED
    # 2. human
    res = resolve_reviewer(
        ReviewerSelector(), no_manager, mode=ReviewMode.PRE_ACTION, fail_closed=True
    )
    assert res.outcome == "human_required"
    # 3. skip (optional policy) or fail closed (mandatory blocking policy)
    no_human = ReviewerSelector(fallback_to_human=False)
    assert (
        resolve_reviewer(
            no_human, no_manager, mode=ReviewMode.POST_ACTION, fail_closed=True
        ).outcome
        == "skipped"
    )
    assert (
        resolve_reviewer(
            no_human, no_manager, mode=ReviewMode.PRE_ACTION, fail_closed=False
        ).outcome
        == "skipped"
    )
    assert (
        resolve_reviewer(no_human, no_manager, mode=ReviewMode.PRE_ACTION, fail_closed=True).outcome
        == "fail_closed"
    )
    assert (
        resolve_reviewer(
            no_human, no_manager, mode=ReviewMode.BEFORE_CLOSE, fail_closed=True
        ).outcome
        == "fail_closed"
    )


def test_inactive_manager_is_not_a_reviewer() -> None:
    res = resolve_reviewer(
        ReviewerSelector(fallback_to_human=False),
        candidates(active_agents={MANAGER: False}),
        mode=ReviewMode.PRE_ACTION,
        fail_closed=True,
    )
    assert res.outcome == "fail_closed"


def test_policy_spec_from_row_ignores_malformed_rows() -> None:
    good = policy_spec_from_row(
        policy_id="p",
        mode="pre_action",
        scope_kind="workspace",
        scope_id=None,
        scope_key=None,
        enabled=True,
        conditions_json=[{"kind": "destructive_action"}],
        reviewer_selector_json={"kind": "human"},
        fail_closed=True,
        priority=5,
    )
    assert good is not None and good.reviewer.kind == "human"
    bad = policy_spec_from_row(
        policy_id="p",
        mode="nope",
        scope_kind="workspace",
        scope_id=None,
        scope_key=None,
        enabled=True,
        conditions_json=[],
        reviewer_selector_json={},
        fail_closed=False,
        priority=1,
    )
    assert bad is None
    unknown_condition = policy_spec_from_row(
        policy_id="p",
        mode="pre_action",
        scope_kind="workspace",
        scope_id=None,
        scope_key=None,
        enabled=True,
        conditions_json=[{"kind": "teleport"}],
        reviewer_selector_json={},
        fail_closed=False,
        priority=1,
    )
    assert unknown_condition is None
