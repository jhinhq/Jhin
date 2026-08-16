"""Team business logic: workspace-scoped CRUD with nesting cycle prevention.

Every query filters by workspace_id (plan 20.2 / security invariant 48.4).
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
from jhin_db.models import Agent, Team


async def list_teams(db: AsyncSession, workspace_id: UUID) -> list[Team]:
    rows = await db.scalars(
        select(Team).where(Team.workspace_id == workspace_id).order_by(Team.created_at)
    )
    return list(rows)


async def get_team(db: AsyncSession, workspace_id: UUID, team_id: UUID) -> Team:
    team = await db.scalar(
        select(Team).where(Team.id == team_id, Team.workspace_id == workspace_id)
    )
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return team


async def _validate_parent(
    db: AsyncSession, workspace_id: UUID, parent_team_id: UUID | None
) -> None:
    if parent_team_id is None:
        return
    exists = await db.scalar(
        select(Team.id).where(Team.id == parent_team_id, Team.workspace_id == workspace_id)
    )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="parent_team_id does not reference a team in this workspace",
        )


async def _validate_manager_agent(
    db: AsyncSession, workspace_id: UUID, manager_agent_id: UUID | None
) -> None:
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


async def _check_nesting_cycle(
    db: AsyncSession, workspace_id: UUID, team_id: UUID, new_parent_id: UUID | None
) -> None:
    """Server-side team-nesting cycle prevention (plan 17.4)."""
    result = await db.execute(
        select(Team.id, Team.parent_team_id).where(Team.workspace_id == workspace_id)
    )
    parents: dict[UUID, UUID | None] = {row[0]: row[1] for row in result.all()}
    if would_create_cycle(team_id, new_parent_id, parents):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This parent would create a cycle in the team hierarchy",
        )


async def create_team(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Team:
    await _validate_parent(db, ctx.workspace_id, values.get("parent_team_id"))
    await _validate_manager_agent(db, ctx.workspace_id, values.get("manager_agent_id"))
    team = Team(workspace_id=ctx.workspace_id, **values)
    db.add(team)
    await db.flush()
    audit.record(
        db,
        action="team.created",
        target_type="team",
        target_id=team.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": team.name},
    )
    await db.commit()
    return team


async def update_team(
    db: AsyncSession,
    ctx: WorkspaceContext,
    team_id: UUID,
    *,
    changes: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Team:
    team = await get_team(db, ctx.workspace_id, team_id)
    if "parent_team_id" in changes:
        await _validate_parent(db, ctx.workspace_id, changes["parent_team_id"])
        await _check_nesting_cycle(db, ctx.workspace_id, team.id, changes["parent_team_id"])
    if "manager_agent_id" in changes:
        await _validate_manager_agent(db, ctx.workspace_id, changes["manager_agent_id"])
    for field, value in changes.items():
        setattr(team, field, value)
    audit.record(
        db,
        action="team.updated",
        target_type="team",
        target_id=team.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    await db.commit()
    return team


async def delete_team(
    db: AsyncSession,
    ctx: WorkspaceContext,
    team_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    team = await get_team(db, ctx.workspace_id, team_id)
    audit.record(
        db,
        action="team.deleted",
        target_type="team",
        target_id=team.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": team.name},
    )
    # Child teams and member agents are detached (FK SET NULL), not deleted.
    await db.delete(team)
    await db.commit()
