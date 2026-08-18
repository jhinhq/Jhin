"""Route handlers for /api/v1/workspaces/{workspace_id}/teams."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.teams import service
from jhin_api.teams.schemas import TeamCreate, TeamMembershipGroups, TeamOut, TeamUpdate
from jhin_db.models import Team

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/teams",
    tags=["teams"],
    dependencies=[Depends(csrf_protect)],
)


def _out(team: Team, memberships: TeamMembershipGroups | None = None) -> TeamOut:
    result = TeamOut.model_validate(team, from_attributes=True)
    return result if memberships is None else result.model_copy(update={"memberships": memberships})


@router.get("")
async def list_teams(ctx: ViewerCtx, db: DbSession) -> list[TeamOut]:
    return [_out(team) for team in await service.list_teams(db, ctx.workspace_id)]


@router.get("/{team_id}")
async def get_team(team_id: UUID, ctx: ViewerCtx, db: DbSession) -> TeamOut:
    team = await service.get_team(db, ctx.workspace_id, team_id)
    memberships = await service.get_team_memberships(db, ctx.workspace_id, team_id)
    return _out(team, memberships)


@router.post("", status_code=201)
async def create_team(
    payload: TeamCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> TeamOut:
    team = await service.create_team(
        db,
        ctx,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(team)


@router.patch("/{team_id}")
async def update_team(
    team_id: UUID, payload: TeamUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> TeamOut:
    team = await service.update_team(
        db,
        ctx,
        team_id,
        changes=payload.model_dump(exclude_unset=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _out(team)


@router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_team(
        db, ctx, team_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
