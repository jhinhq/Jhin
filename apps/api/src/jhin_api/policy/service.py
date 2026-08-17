"""Capability grants and approval-policy business logic (plan 6.6, 42, 23).

Grants are the only way an agent gains a capability, and only workspace
admins may create or revoke them through this API — agents themselves have no
tool that can reach these endpoints (plan 21.7). Every change is audited as
``agent.permission.granted`` / ``agent.permission.revoked``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service as agents_service
from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AgentCapabilityGrant
from jhin_policy import ApprovalPreset, PolicyRule, matching_preset, rules_for_preset


async def list_grants(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[AgentCapabilityGrant]:
    await agents_service.get_agent(db, workspace_id, agent_id)  # 404 if missing
    rows = await db.scalars(
        select(AgentCapabilityGrant)
        .where(
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == workspace_id,
        )
        .order_by(AgentCapabilityGrant.created_at)
    )
    return list(rows)


async def create_grant(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    capability: str,
    scope: dict[str, str],
    effect: str,
    request_id: UUID,
    ip_hash: str,
) -> AgentCapabilityGrant:
    await agents_service.get_agent(db, ctx.workspace_id, agent_id)

    duplicate = await db.scalar(
        select(AgentCapabilityGrant.id).where(
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == ctx.workspace_id,
            AgentCapabilityGrant.capability == capability,
            AgentCapabilityGrant.effect == effect,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A '{effect}' grant for '{capability}' already exists on this agent",
        )

    grant = AgentCapabilityGrant(
        workspace_id=ctx.workspace_id,
        agent_id=agent_id,
        capability=capability,
        scope_json=dict(scope),
        effect=effect,
    )
    db.add(grant)
    await db.flush()
    audit.record(
        db,
        action="agent.permission.granted",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "grant_id": str(grant.id),
            "capability": capability,
            "scope": scope,
            "effect": effect,
        },
    )
    await db.commit()
    return grant


async def revoke_grant(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    grant_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    grant = await db.scalar(
        select(AgentCapabilityGrant).where(
            AgentCapabilityGrant.id == grant_id,
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == ctx.workspace_id,
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    audit.record(
        db,
        action="agent.permission.revoked",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "grant_id": str(grant.id),
            "capability": grant.capability,
            "effect": grant.effect,
        },
    )
    await db.delete(grant)
    await db.commit()


def parse_rules(raw: list[Any]) -> list[PolicyRule]:
    rules: list[PolicyRule] = []
    for item in raw:
        try:
            rules.append(PolicyRule.model_validate(item))
        except ValueError:
            continue  # malformed persisted rules are skipped, not fatal
    return rules


async def get_policy(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    return await agents_service.get_agent(db, workspace_id, agent_id)


async def update_policy(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    preset: str | None,
    rules: list[dict[str, Any]] | None,
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    """Persist explicit rules; a preset is expanded before persisting (plan 42)."""
    agent = await agents_service.get_agent(db, ctx.workspace_id, agent_id)

    if preset is not None and rules is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide either a preset or explicit rules, not both",
        )
    if preset is not None:
        new_rules = list(rules_for_preset(ApprovalPreset(preset)))
    elif rules is not None:
        new_rules = [PolicyRule.model_validate(rule) for rule in rules]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide a preset or explicit rules",
        )

    agent.approval_policy_json = [rule.model_dump(mode="json") for rule in new_rules]
    audit.record(
        db,
        action="agent.policy.updated",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "preset": preset,
            "rules": agent.approval_policy_json,
        },
    )
    await db.commit()
    return agent


def preset_of(rules: list[PolicyRule]) -> str | None:
    preset = matching_preset(rules)
    return preset.value if preset is not None else None
