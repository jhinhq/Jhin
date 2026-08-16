"""Agent business logic: workspace-scoped CRUD, manager-chain cycle
prevention, and pause/resume actions (plan 6.5, 19).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.org.hierarchy import would_create_cycle
from jhin_api.slugs import slugify, with_suffix
from jhin_db.models import Agent, ModelProfile, Team
from jhin_domain import AgentStatus


async def list_agents(db: AsyncSession, workspace_id: UUID) -> list[Agent]:
    rows = await db.scalars(
        select(Agent).where(Agent.workspace_id == workspace_id).order_by(Agent.created_at)
    )
    return list(rows)


async def get_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


async def _unique_slug(db: AsyncSession, workspace_id: UUID, name: str) -> str:
    slug = slugify(name)
    taken = await db.scalar(
        select(Agent.id).where(Agent.workspace_id == workspace_id, Agent.slug == slug)
    )
    return with_suffix(slug) if taken else slug


async def _validate_team(db: AsyncSession, workspace_id: UUID, team_id: UUID | None) -> None:
    if team_id is None:
        return
    exists = await db.scalar(
        select(Team.id).where(Team.id == team_id, Team.workspace_id == workspace_id)
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="team_id does not reference a team in this workspace",
        )


async def _validate_manager(
    db: AsyncSession,
    workspace_id: UUID,
    agent_id: UUID | None,
    manager_agent_id: UUID | None,
) -> None:
    """Manager must exist in this workspace; the chain must stay acyclic."""
    if manager_agent_id is None:
        return
    exists = await db.scalar(
        select(Agent.id).where(Agent.id == manager_agent_id, Agent.workspace_id == workspace_id)
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manager_agent_id does not reference an agent in this workspace",
        )
    if agent_id is None:
        return  # a brand-new agent has no subordinates, so no cycle is possible
    result = await db.execute(
        select(Agent.id, Agent.manager_agent_id).where(Agent.workspace_id == workspace_id)
    )
    managers: dict[UUID, UUID | None] = {row[0]: row[1] for row in result.all()}
    if would_create_cycle(agent_id, manager_agent_id, managers):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This manager would create a cycle in the reporting chain",
        )


async def _validate_model_profile(
    db: AsyncSession, workspace_id: UUID, profile_id: UUID | None
) -> None:
    if profile_id is None:
        return
    exists = await db.scalar(
        select(ModelProfile.id).where(
            ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id
        )
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="model_profile_id does not reference a model profile in this workspace",
        )


async def create_agent(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    await _validate_team(db, ctx.workspace_id, values.get("team_id"))
    await _validate_manager(db, ctx.workspace_id, None, values.get("manager_agent_id"))
    await _validate_model_profile(db, ctx.workspace_id, values.get("model_profile_id"))
    agent = Agent(
        workspace_id=ctx.workspace_id,
        slug=await _unique_slug(db, ctx.workspace_id, values["name"]),
        **values,
    )
    db.add(agent)
    await db.flush()
    audit.record(
        db,
        action="agent.created",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": agent.name, "slug": agent.slug},
    )
    await db.commit()
    return agent


async def update_agent(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    agent = await get_agent(db, ctx.workspace_id, agent_id)
    if "team_id" in changes:
        await _validate_team(db, ctx.workspace_id, changes["team_id"])
    if "manager_agent_id" in changes:
        await _validate_manager(db, ctx.workspace_id, agent.id, changes["manager_agent_id"])
    if "model_profile_id" in changes:
        await _validate_model_profile(db, ctx.workspace_id, changes["model_profile_id"])
    for field, value in changes.items():
        setattr(agent, field, value)
    audit.record(
        db,
        action="agent.updated",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    await db.commit()
    return agent


async def delete_agent(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    agent = await get_agent(db, ctx.workspace_id, agent_id)
    audit.record(
        db,
        action="agent.deleted",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": agent.name, "slug": agent.slug},
    )
    # Subordinates and managed teams are detached (FK SET NULL), not deleted.
    await db.delete(agent)
    await db.commit()


async def set_agent_status(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    new_status: AgentStatus,
    action: str,
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    """Pause/resume by flipping status; run control arrives with Temporal in
    Phase 3+."""
    agent = await get_agent(db, ctx.workspace_id, agent_id)
    previous = agent.status
    agent.status = new_status.value
    audit.record(
        db,
        action=action,
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"from_status": previous, "to_status": new_status.value},
    )
    await db.commit()
    return agent
