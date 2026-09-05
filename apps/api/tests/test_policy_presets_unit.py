"""Changing the approval preset must not quietly change what needs approval.

The agent wizard writes two kinds of rule for a coding agent: the preset's
risk-level rules, and one per-capability rule that keeps a human in front of
``cli.repository.push`` even under Autonomous (``apps/web/lib/wizard.ts``).
Both places in the UI that change the mode — the chat sidebar's quick controls
and the org Tools & Access tab — send ``PUT /policy {"preset": …}``, which is
this function. Expanding the preset over the whole list took the gate with it,
and the click that did it is one an operator makes for an unrelated reason.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.policy import service
from jhin_db.models import Agent
from jhin_domain import ActorType, new_uuid7
from jhin_policy import (
    ApprovalPreset,
    DecisionType,
    Grant,
    PolicyRule,
    RiskLevel,
    RuleAction,
    ToolDefinition,
    evaluate,
    rules_for_preset,
)


class _Payload(BaseModel):
    """Stand-in schema: these tests exercise policy, never a tool body."""


PUSH = ToolDefinition(
    name="cli.repository.push",
    description="",
    risk=RiskLevel.ELEVATED,
    input_model=_Payload,
    output_model=_Payload,
    required_capability="cli.repository.push",
    supports_approval=True,
)
GATE = {"capability": "cli.repository.push", "risk": None, "action": "approval"}


async def _coding_agent(session: AsyncSession, ctx: WorkspaceContext) -> Agent:
    """An agent as the wizard creates it with the Code-editing bundle."""
    agent = Agent(
        workspace_id=ctx.workspace_id,
        name="Coder",
        slug=f"coder-{new_uuid7().hex[:8]}",
        role_title="",
        description="",
        system_prompt="",
        approval_policy_json=[
            GATE,
            *[rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)],
        ],
    )
    session.add(agent)
    await session.flush()
    return agent


def _push_decision(agent: Agent) -> DecisionType:
    return evaluate(
        PUSH,
        grants=[Grant(capability="cli.repository.push", scope={})],
        rules=service.parse_rules(list(agent.approval_policy_json)),
    ).decision


async def _set_preset(
    session: AsyncSession, ctx: WorkspaceContext, agent: Agent, preset: str
) -> Agent:
    return await service.update_policy(
        session,
        ctx,
        agent.id,
        preset=preset,
        rules=None,
        request_id=new_uuid7(),
        ip_hash="0" * 8,
    )


@pytest.mark.parametrize("preset", [preset.value for preset in ApprovalPreset])
async def test_changing_the_preset_keeps_the_push_gate(
    preset: str, session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Autonomous is the case that matters — it runs ELEVATED tools without
    asking, so the capability rule is the only thing left in front of the
    first action that leaves the sandbox."""
    agent = await _coding_agent(session, admin_ctx)
    assert _push_decision(agent) is DecisionType.REQUIRE_APPROVAL

    updated = await _set_preset(session, admin_ctx, agent, preset)

    assert GATE in updated.approval_policy_json
    assert _push_decision(updated) is DecisionType.REQUIRE_APPROVAL
    # And the preset really did apply to the risk levels it speaks for.
    assert service.preset_of(service.parse_rules(list(updated.approval_policy_json))) == preset


async def test_the_kept_rule_stays_ahead_of_the_risk_rules(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Rules are first-match: behind ``*``/elevated/auto the capability rule
    would still be persisted and never reached."""
    agent = await _coding_agent(session, admin_ctx)

    updated = await _set_preset(session, admin_ctx, agent, "autonomous")

    assert updated.approval_policy_json[0] == GATE
    assert updated.approval_policy_json[1:] == [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.AUTONOMOUS)
    ]


async def test_a_second_preset_change_does_not_duplicate_the_kept_rule(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _coding_agent(session, admin_ctx)

    await _set_preset(session, admin_ctx, agent, "autonomous")
    updated = await _set_preset(session, admin_ctx, agent, "restricted")

    assert [rule for rule in updated.approval_policy_json if rule == GATE] == [GATE]


async def test_explicit_rules_still_mean_exactly_what_they_say(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """Preserving rules across a *preset* change is not the same as making
    them unremovable: sending the list is how a rule is deliberately dropped,
    and that path is untouched."""
    agent = await _coding_agent(session, admin_ctx)

    updated = await service.update_policy(
        session,
        admin_ctx,
        agent.id,
        preset=None,
        rules=[rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)],
        request_id=new_uuid7(),
        ip_hash="0" * 8,
    )

    assert GATE not in updated.approval_policy_json
    assert _push_decision(updated) is DecisionType.REQUIRE_APPROVAL  # ELEVATED under balanced


async def test_an_agent_with_no_rules_yet_gets_exactly_the_preset(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = Agent(
        workspace_id=admin_ctx.workspace_id,
        name="Plain",
        slug=f"plain-{new_uuid7().hex[:8]}",
        role_title="",
        description="",
        system_prompt="",
        approval_policy_json=[],
    )
    session.add(agent)
    await session.flush()

    updated = await _set_preset(session, admin_ctx, agent, "balanced")

    assert updated.approval_policy_json == [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)
    ]


async def test_a_malformed_persisted_rule_is_not_carried_forward(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """``parse_rules`` already drops what it cannot read; keeping rules across
    a preset change must not resurrect it as something else."""
    agent = await _coding_agent(session, admin_ctx)
    agent.approval_policy_json = [
        {"capability": "cli.repository.push"},
        *agent.approval_policy_json,
    ]
    await session.flush()

    updated = await _set_preset(session, admin_ctx, agent, "balanced")

    assert all(PolicyRule.model_validate(rule) for rule in updated.approval_policy_json)
    assert updated.approval_policy_json.count(GATE) == 1


async def test_a_rule_the_bundle_wrote_survives_a_later_preset_change(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    """``ensure_capability_rules`` is what the bundle endpoint writes with;
    the gate it prepends is a per-capability rule like the wizard's, so the
    next mode switch keeps it — and asking twice adds nothing."""
    agent = Agent(
        workspace_id=admin_ctx.workspace_id,
        name="Bundled",
        slug=f"bundled-{new_uuid7().hex[:8]}",
        role_title="",
        description="",
        system_prompt="",
        approval_policy_json=[
            rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)
        ],
    )
    session.add(agent)
    await session.flush()
    gate = PolicyRule(capability="cli.repository.push", risk=None, action=RuleAction.APPROVAL)

    added = await service.ensure_capability_rules(
        session,
        admin_ctx,
        agent,
        [gate],
        request_id=new_uuid7(),
        ip_hash=None,
        actor_type=ActorType.USER,
        extra_metadata=None,
        bundle_id="code-editing",
    )
    await session.commit()

    assert added == [gate]
    assert agent.approval_policy_json[0] == GATE
    assert _push_decision(agent) is DecisionType.REQUIRE_APPROVAL

    updated = await _set_preset(session, admin_ctx, agent, "autonomous")
    assert updated.approval_policy_json[0] == GATE
    assert _push_decision(updated) is DecisionType.REQUIRE_APPROVAL

    again = await service.ensure_capability_rules(
        session,
        admin_ctx,
        updated,
        [gate],
        request_id=new_uuid7(),
        ip_hash=None,
        actor_type=ActorType.USER,
        extra_metadata=None,
        bundle_id="code-editing",
    )
    assert again == []
    assert updated.approval_policy_json.count(GATE) == 1
