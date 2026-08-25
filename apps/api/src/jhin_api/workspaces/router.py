"""Route handlers for /api/v1/workspaces (incl. members). Logic in service."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, CurrentAuth, DbSession, OwnerCtx, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.workspaces import service
from jhin_api.workspaces.schemas import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    WorkspaceCreate,
    WorkspaceDeletionSummary,
    WorkspaceOut,
    WorkspaceUpdate,
)
from jhin_db.models import User, Workspace, WorkspaceMembership
from jhin_domain import WorkspaceRole

router = APIRouter(
    prefix="/api/v1/workspaces", tags=["workspaces"], dependencies=[Depends(csrf_protect)]
)


def _workspace_out(workspace: Workspace) -> WorkspaceOut:
    return WorkspaceOut.model_validate(workspace, from_attributes=True)


def _member_out(membership: WorkspaceMembership, user: User) -> MemberOut:
    return MemberOut(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=WorkspaceRole(membership.role),
        created_at=membership.created_at,
    )


@router.get("")
async def list_workspaces(db: DbSession, auth: CurrentAuth) -> list[WorkspaceOut]:
    return [_workspace_out(w) for w in await service.list_for_user(db, auth.user.id)]


@router.post("", status_code=201)
async def create_workspace(
    payload: WorkspaceCreate, request: Request, db: DbSession, auth: CurrentAuth
) -> WorkspaceOut:
    workspace = await service.create(
        db,
        name=payload.name,
        default_timezone=payload.default_timezone,
        creator_id=auth.user.id,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _workspace_out(workspace)


@router.get("/{workspace_id}")
async def get_workspace(ctx: ViewerCtx, db: DbSession) -> WorkspaceOut:
    return _workspace_out(await service.get(db, ctx.workspace_id))


@router.patch("/{workspace_id}")
async def update_workspace(
    payload: WorkspaceUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> WorkspaceOut:
    workspace = await service.update(
        db,
        ctx,
        changes=payload.model_dump(exclude_unset=True, exclude_none=True),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _workspace_out(workspace)


@router.get("/{workspace_id}/deletion-summary")
async def workspace_deletion_summary(ctx: OwnerCtx, db: DbSession) -> WorkspaceDeletionSummary:
    """What ``DELETE /workspaces/{workspace_id}`` would destroy, counted now.

    Owner-only for the same reason the delete is: nobody else can act on it,
    and a whole-workspace inventory is not a viewer's business. Read-only —
    calling it changes nothing.
    """
    workspace = await service.get(db, ctx.workspace_id)
    counts = await service.deletion_summary(db, ctx.workspace_id)
    return WorkspaceDeletionSummary(workspace_id=workspace.id, name=workspace.name, **counts)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(request: Request, ctx: OwnerCtx, db: DbSession) -> None:
    await service.delete(db, ctx, request_id=req_id(request), ip_hash=ip_hash(request))


@router.get("/{workspace_id}/members")
async def list_members(ctx: ViewerCtx, db: DbSession) -> list[MemberOut]:
    rows = await service.list_members(db, ctx.workspace_id)
    return [_member_out(membership, user) for membership, user in rows]


@router.post("/{workspace_id}/members", status_code=201)
async def add_member(
    payload: MemberCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> MemberOut:
    membership, user = await service.add_member(
        db,
        ctx,
        email=payload.email,
        role=payload.role,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _member_out(membership, user)


@router.patch("/{workspace_id}/members/{membership_id}")
async def update_member(
    membership_id: UUID,
    payload: MemberUpdate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> MemberOut:
    membership, user = await service.update_member_role(
        db,
        ctx,
        membership_id,
        role=payload.role,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return _member_out(membership, user)


@router.delete("/{workspace_id}/members/{membership_id}", status_code=204)
async def remove_member(
    membership_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> None:
    await service.remove_member(
        db, ctx, membership_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
