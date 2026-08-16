"""Route handlers for /api/v1/workspaces/{workspace_id}/agents.

CRUD requires admin; pause/resume are operational actions available to
members (plan 20.2: members operate agents, admins manage them).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.agents import service
from jhin_api.agents.schemas import AgentCreate, AgentOut, AgentUpdate
from jhin_api.deps import AdminCtx, DbSession, MemberCtx, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import Agent
from jhin_domain import AgentStatus

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/agents",
    tags=["agents"],
    dependencies=[Depends(csrf_protect)],
)


def _out(agent: Agent) -> AgentOut:
    return AgentOut.model_validate(agent, from_attributes=True)


@router.get("")
async def list_agents(ctx: ViewerCtx, db: DbSession) -> list[AgentOut]:
    return [_out(agent) for agent in await service.list_agents(db, ctx.workspace_id)]


@router.get("/{agent_id}")
async def get_agent(agent_id: UUID, ctx: ViewerCtx, db: DbSession) -> AgentOut:
    return _out(await service.get_agent(db, ctx.workspace_id, agent_id))


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
    return _out(agent)


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
    return _out(agent)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_agent(
        db, ctx, agent_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: UUID, request: Request, ctx: MemberCtx, db: DbSession) -> AgentOut:
    agent = await service.set_agent_status(
        db,
        ctx,
        agent_id,
        new_status=AgentStatus.PAUSED,
        action="agent.paused",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(agent)


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: UUID, request: Request, ctx: MemberCtx, db: DbSession) -> AgentOut:
    agent = await service.set_agent_status(
        db,
        ctx,
        agent_id,
        new_status=AgentStatus.ACTIVE,
        action="agent.resumed",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(agent)
