"""Route handlers for /api/v1/workspaces/{workspace_id}/agents.

CRUD requires admin; pause/resume are operational actions available to
members (plan 20.2: members operate agents, admins manage them).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.agents import service
from jhin_api.agents.schemas import (
    AgentCreate,
    AgentMembershipOut,
    AgentOut,
    AgentRelationshipOut,
    AgentUpdate,
    MembershipReplace,
    RelationshipCreate,
)
from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.personas import service as personas_service
from jhin_api.personas.schemas import AgentPersonaSummary
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import Agent, AgentRelationship, AgentTeamMembership
from jhin_domain import AgentStatus

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/agents",
    tags=["agents"],
    dependencies=[Depends(csrf_protect)],
)


def _membership_out(membership: AgentTeamMembership) -> AgentMembershipOut:
    return AgentMembershipOut(
        id=membership.id,
        workspace_id=membership.workspace_id,
        agent_id=membership.agent_id,
        team_id=membership.team_id,
        is_primary=membership.is_primary,
        role_label=membership.role_label,
        joined_at=membership.joined_at,
        left_at=membership.left_at,
        state="inactive" if membership.left_at is not None else "active",
    )


def _relationship_out(relationship: AgentRelationship) -> AgentRelationshipOut:
    return AgentRelationshipOut.model_validate(relationship, from_attributes=True)


async def _out(db: DbSession, agent: Agent) -> AgentOut:
    result = AgentOut.model_validate(agent, from_attributes=True)
    memberships = await service.list_memberships(db, agent.workspace_id, agent.id)
    relationships = await service.list_relationships(db, agent.workspace_id, agent.id)
    persona = await personas_service.persona_for_agent(db, agent)
    return result.model_copy(
        update={
            "memberships": [_membership_out(row) for row in memberships],
            "relationships": [_relationship_out(row) for row in relationships],
            "persona": (AgentPersonaSummary.from_record(persona) if persona is not None else None),
        }
    )


@router.get("")
async def list_agents(ctx: ViewerCtx, db: DbSession) -> list[AgentOut]:
    return [await _out(db, agent) for agent in await service.list_agents(db, ctx.workspace_id)]


@router.get("/{agent_id}")
async def get_agent(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> AgentOut:
    return await _out(db, await service.get_agent(db, ctx.workspace_id, agent_id))


@router.post("", status_code=201)
async def create_agent(
    payload: AgentCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> AgentOut:
    agent = await service.create_agent(
        db,
        ctx,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await _out(db, agent)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: UUID, payload: AgentUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> AgentOut:
    agent = await service.update_agent(
        db,
        ctx,
        agent_id,
        changes=payload.model_dump(exclude_unset=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await _out(db, agent)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_agent(
        db, ctx, agent_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@router.get("/{agent_id}/memberships")
async def list_memberships(
    agent_id: UUID, ctx: ViewerCtx, db: DbSession
) -> list[AgentMembershipOut]:
    memberships = await service.list_memberships(db, ctx.workspace_id, agent_id)
    return [_membership_out(row) for row in memberships]


@router.put("/{agent_id}/memberships")
async def replace_memberships(
    agent_id: UUID,
    payload: MembershipReplace,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> list[AgentMembershipOut]:
    memberships = await service.replace_memberships(
        db,
        ctx,
        agent_id,
        primary_team_id=payload.primary_team_id,
        secondary_team_ids=payload.secondary_team_ids,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return [_membership_out(row) for row in memberships]


@router.post("/{agent_id}/relationships", status_code=201)
async def create_relationship(
    agent_id: UUID,
    payload: RelationshipCreate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> AgentRelationshipOut:
    relationship = await service.create_relationship(
        db,
        ctx,
        agent_id,
        target_agent_id=payload.target_agent_id,
        kind=payload.kind,
        purpose=payload.purpose,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _relationship_out(relationship)


@router.delete("/{agent_id}/relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    agent_id: UUID,
    relationship_id: UUID,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> None:
    await service.delete_relationship(
        db,
        ctx,
        agent_id,
        relationship_id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> AgentOut:
    agent = await service.set_agent_status(
        db,
        ctx,
        agent_id,
        new_status=AgentStatus.PAUSED,
        action="agent.paused",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await _out(db, agent)


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> AgentOut:
    agent = await service.set_agent_status(
        db,
        ctx,
        agent_id,
        new_status=AgentStatus.ACTIVE,
        action="agent.resumed",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return await _out(db, agent)
