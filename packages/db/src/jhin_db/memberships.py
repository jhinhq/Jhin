"""Current membership authority shared by directory, variables and memory.

A recorded departure defeats a stale legacy primary-team pointer. Legacy
records without a membership row remain compatible until explicitly managed.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentTeamMembership


async def active_team_ids(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> list[UUID]:
    legacy = await db.scalar(
        select(Agent.team_id).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    rows = (
        await db.execute(
            select(AgentTeamMembership.team_id, AgentTeamMembership.left_at).where(
                AgentTeamMembership.workspace_id == workspace_id,
                AgentTeamMembership.agent_id == agent_id,
            )
        )
    ).all()
    active = {row.team_id for row in rows if row.left_at is None}
    if legacy is not None and not any(row.team_id == legacy for row in rows):
        active.add(legacy)
    return sorted(active)


async def primary_team_id(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> UUID | None:
    active = await db.scalar(
        select(AgentTeamMembership.team_id)
        .where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id == agent_id,
            AgentTeamMembership.left_at.is_(None),
            AgentTeamMembership.is_primary.is_(True),
        )
        .order_by(AgentTeamMembership.team_id)
        .limit(1)
    )
    if active is not None:
        return active
    legacy = await db.scalar(
        select(Agent.team_id).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    return legacy if legacy in await active_team_ids(db, workspace_id, agent_id) else None
