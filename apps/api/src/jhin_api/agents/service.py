"""Agent business logic: workspace-scoped CRUD, manager-chain cycle
prevention, and pause/resume actions (plan 6.5, 19).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.org.hierarchy import would_create_cycle
from jhin_api.slugs import slugify, with_suffix
from jhin_db.models import (
    Agent,
    AgentRelationship,
    AgentTeamMembership,
    ModelProfile,
    Team,
)
from jhin_domain import AgentStatus, AvatarKind


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


async def _get_locked_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(
        select(Agent)
        .where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        .with_for_update()
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


async def _lock_workspace_agents(db: AsyncSession, workspace_id: UUID) -> dict[UUID, Agent]:
    """Serialize reporting-line changes on a stable workspace-scoped lock set."""
    agents = list(
        await db.scalars(
            select(Agent)
            .where(Agent.workspace_id == workspace_id)
            .order_by(Agent.id)
            .with_for_update()
        )
    )
    return {agent.id: agent for agent in agents}


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


async def _validate_new_agent_manager(
    db: AsyncSession,
    workspace_id: UUID,
    manager_agent_id: UUID | None,
) -> None:
    if manager_agent_id is None:
        return
    await get_agent(db, workspace_id, manager_agent_id)


def _validate_manager_change(
    agent_id: UUID,
    manager_agent_id: UUID | None,
    workspace_agents: dict[UUID, Agent],
) -> None:
    if manager_agent_id is None:
        return
    if manager_agent_id not in workspace_agents:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    managers = {
        current_agent.id: current_agent.manager_agent_id
        for current_agent in workspace_agents.values()
    }
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


def _validate_membership_duplicates(
    primary_team_id: UUID | None, secondary_team_ids: list[UUID]
) -> None:
    if len(secondary_team_ids) != len(set(secondary_team_ids)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A team may appear only once in an active membership set",
        )
    if primary_team_id is not None and primary_team_id in secondary_team_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The primary team cannot also be a secondary team",
        )


async def _validate_membership_teams(
    db: AsyncSession,
    workspace_id: UUID,
    primary_team_id: UUID | None,
    secondary_team_ids: list[UUID],
) -> None:
    requested = set(secondary_team_ids)
    if primary_team_id is not None:
        requested.add(primary_team_id)
    if not requested:
        return
    found = set(
        await db.scalars(
            select(Team.id).where(Team.workspace_id == workspace_id, Team.id.in_(requested))
        )
    )
    if found != requested:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


async def list_memberships(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[AgentTeamMembership]:
    await get_agent(db, workspace_id, agent_id)
    rows = await db.scalars(
        select(AgentTeamMembership)
        .where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id == agent_id,
            AgentTeamMembership.left_at.is_(None),
        )
        .order_by(AgentTeamMembership.is_primary.desc(), AgentTeamMembership.joined_at)
    )
    return list(rows)


async def _replace_memberships_locked(
    db: AsyncSession,
    workspace_id: UUID,
    agent_id: UUID,
    *,
    primary_team_id: UUID | None,
    secondary_team_ids: list[UUID],
) -> tuple[Agent, list[AgentTeamMembership]]:
    agent = await _get_locked_agent(db, workspace_id, agent_id)
    _validate_membership_duplicates(primary_team_id, secondary_team_ids)
    await _validate_membership_teams(db, workspace_id, primary_team_id, secondary_team_ids)

    current = list(
        await db.scalars(
            select(AgentTeamMembership)
            .where(
                AgentTeamMembership.workspace_id == workspace_id,
                AgentTeamMembership.agent_id == agent_id,
                AgentTeamMembership.left_at.is_(None),
            )
            .with_for_update()
        )
    )
    now = datetime.now(UTC)
    for membership in current:
        membership.left_at = now
    agent.team_id = primary_team_id
    # End old rows before inserting replacements so the partial unique indexes
    # never observe two active rows during a primary-team change.
    await db.flush()

    replacements = [
        AgentTeamMembership(
            workspace_id=workspace_id,
            agent_id=agent_id,
            team_id=team_id,
            is_primary=is_primary,
        )
        for team_id, is_primary in (
            ([(primary_team_id, True)] if primary_team_id is not None else [])
            + [(team_id, False) for team_id in secondary_team_ids]
        )
    ]
    db.add_all(replacements)
    await db.flush()
    return agent, replacements


async def replace_memberships(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    primary_team_id: UUID | None,
    secondary_team_ids: list[UUID],
    request_id: UUID,
    ip_hash: str,
) -> list[AgentTeamMembership]:
    try:
        _agent, memberships = await _replace_memberships_locked(
            db,
            ctx.workspace_id,
            agent_id,
            primary_team_id=primary_team_id,
            secondary_team_ids=secondary_team_ids,
        )
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Membership replacement conflicts with active topology",
        ) from exc
    audit.record(
        db,
        action="agent.memberships.updated",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "primary_team_id": str(primary_team_id) if primary_team_id else None,
            "secondary_team_ids": [str(team_id) for team_id in secondary_team_ids],
        },
    )
    await db.commit()
    return memberships


async def list_relationships(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[AgentRelationship]:
    await get_agent(db, workspace_id, agent_id)
    rows = await db.scalars(
        select(AgentRelationship)
        .where(
            AgentRelationship.workspace_id == workspace_id,
            AgentRelationship.status == "active",
            or_(
                AgentRelationship.source_agent_id == agent_id,
                AgentRelationship.target_agent_id == agent_id,
            ),
        )
        .order_by(AgentRelationship.created_at, AgentRelationship.id)
    )
    return list(rows)


async def create_relationship(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    *,
    target_agent_id: UUID,
    kind: str,
    purpose: str,
    request_id: UUID,
    ip_hash: str,
) -> AgentRelationship:
    await get_agent(db, ctx.workspace_id, agent_id)
    await get_agent(db, ctx.workspace_id, target_agent_id)
    if agent_id == target_agent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An agent cannot have a relationship with itself",
        )
    source_agent_id, stored_target_id = agent_id, target_agent_id
    if kind == "close_collaborator" and source_agent_id > stored_target_id:
        source_agent_id, stored_target_id = stored_target_id, source_agent_id
    duplicate = await db.scalar(
        select(AgentRelationship.id).where(
            AgentRelationship.workspace_id == ctx.workspace_id,
            AgentRelationship.source_agent_id == source_agent_id,
            AgentRelationship.target_agent_id == stored_target_id,
            AgentRelationship.kind == kind,
            AgentRelationship.status == "active",
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This active relationship already exists",
        )
    relationship = AgentRelationship(
        workspace_id=ctx.workspace_id,
        source_agent_id=source_agent_id,
        target_agent_id=stored_target_id,
        kind=kind,
        purpose=purpose,
        status="active",
    )
    db.add(relationship)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This relationship conflicts with active topology",
        ) from exc
    audit.record(
        db,
        action="agent.relationship.created",
        target_type="agent_relationship",
        target_id=relationship.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "source_agent_id": str(source_agent_id),
            "target_agent_id": str(stored_target_id),
            "kind": kind,
        },
    )
    await db.commit()
    return relationship


async def delete_relationship(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    relationship_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    await get_agent(db, ctx.workspace_id, agent_id)
    relationship = await db.scalar(
        select(AgentRelationship)
        .where(
            AgentRelationship.id == relationship_id,
            AgentRelationship.workspace_id == ctx.workspace_id,
            AgentRelationship.status == "active",
            or_(
                AgentRelationship.source_agent_id == agent_id,
                AgentRelationship.target_agent_id == agent_id,
            ),
        )
        .with_for_update()
    )
    if relationship is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relationship not found")
    relationship.status = "inactive"
    await db.flush()
    audit.record(
        db,
        action="agent.relationship.deleted",
        target_type="agent_relationship",
        target_id=relationship.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"kind": relationship.kind},
    )
    await db.commit()


async def create_agent(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    values = dict(values)
    secondary_team_ids: list[UUID] = values.pop("secondary_team_ids", [])
    primary_team_id: UUID | None = values.get("team_id")
    # A shape avatar chosen at creation flips the kind; the schema already
    # guarantees shape and color arrive together and from the fixed lists.
    if values.get("avatar_shape") is not None:
        values["avatar_kind"] = AvatarKind.SHAPE.value
    await _validate_team(db, ctx.workspace_id, primary_team_id)
    _validate_membership_duplicates(primary_team_id, secondary_team_ids)
    await _validate_membership_teams(db, ctx.workspace_id, primary_team_id, secondary_team_ids)
    await _validate_new_agent_manager(db, ctx.workspace_id, values.get("manager_agent_id"))
    await _validate_model_profile(db, ctx.workspace_id, values.get("model_profile_id"))
    agent = Agent(
        workspace_id=ctx.workspace_id,
        slug=await _unique_slug(db, ctx.workspace_id, values["name"]),
        **values,
    )
    db.add(agent)
    await db.flush()
    await _replace_memberships_locked(
        db,
        ctx.workspace_id,
        agent.id,
        primary_team_id=primary_team_id,
        secondary_team_ids=secondary_team_ids,
    )
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
    changes = dict(changes)
    changed_fields = set(changes)
    secondary_team_ids = changes.pop("secondary_team_ids", None)
    topology_changed = "team_id" in changes or secondary_team_ids is not None
    manager_changed = "manager_agent_id" in changes
    workspace_agents: dict[UUID, Agent] | None = None
    if manager_changed:
        workspace_agents = await _lock_workspace_agents(db, ctx.workspace_id)
        agent = workspace_agents.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    elif topology_changed:
        agent = await _get_locked_agent(db, ctx.workspace_id, agent_id)
    else:
        agent = await get_agent(db, ctx.workspace_id, agent_id)
    if "team_id" in changes:
        await _validate_team(db, ctx.workspace_id, changes["team_id"])
    if manager_changed:
        assert workspace_agents is not None
        _validate_manager_change(agent.id, changes["manager_agent_id"], workspace_agents)
    if "model_profile_id" in changes:
        await _validate_model_profile(db, ctx.workspace_id, changes["model_profile_id"])
    primary_team_id = changes.pop("team_id", agent.team_id)
    for field, value in changes.items():
        setattr(agent, field, value)
    if topology_changed:
        if secondary_team_ids is None:
            active = list(
                await db.scalars(
                    select(AgentTeamMembership)
                    .where(
                        AgentTeamMembership.workspace_id == ctx.workspace_id,
                        AgentTeamMembership.agent_id == agent.id,
                        AgentTeamMembership.left_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            secondary_team_ids = [
                row.team_id
                for row in active
                if not row.is_primary and row.team_id != primary_team_id
            ]
        await _replace_memberships_locked(
            db,
            ctx.workspace_id,
            agent.id,
            primary_team_id=primary_team_id,
            secondary_team_ids=secondary_team_ids,
        )
    else:
        await db.flush()
    audit.record(
        db,
        action="agent.updated",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changed_fields)},
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
