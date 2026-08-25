"""Routes for invitations, API keys, and the scope catalog.

Three routers:

* ``invitations_router`` — workspace-scoped, admin+, CSRF-protected;
* ``public_invitations_router`` — unauthenticated preview/accept for the invite
  link. Like ``/auth/login`` it predates any session, so there is no CSRF
  cookie to double-submit; failed attempts are rate limited per token and per
  source address instead;
* ``api_keys_router`` — workspace-scoped key management and usage log.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from jhin_api.access import api_keys as key_service
from jhin_api.access import invitations as invite_service
from jhin_api.access.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyUsageOut,
    ApiKeyUsagePage,
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    InvitationPreview,
    ScopeCatalogOut,
    ScopeCategoryOut,
    ScopeOut,
)
from jhin_api.auth import service as auth_service
from jhin_api.auth.router import SettingsDep, me_response, set_auth_cookies
from jhin_api.auth.schemas import MeResponse
from jhin_api.deps import AdminCtx, DbSession, ViewerCtx, client_ip
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_db.models import ApiKey, ApiKeyUsage, User, WorkspaceInvitation
from jhin_domain import CATEGORIES, CATEGORY_SCOPES, WorkspaceRole, scopes_for_role

invitations_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/invitations",
    tags=["invitations"],
    dependencies=[Depends(csrf_protect)],
)
public_invitations_router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])
api_keys_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/api-keys",
    tags=["api-keys"],
    dependencies=[Depends(csrf_protect)],
)


def _invite_limiter(request: Request) -> LoginRateLimiter | None:
    limiter: LoginRateLimiter | None = getattr(request.app.state, "login_limiter", None)
    return limiter


def _guard_invite_attempt(request: Request, token: str) -> None:
    """Bound how hard one address may hammer the public invitation routes.

    Guessing a token is already hopeless — it is 256 bits — so this is not the
    barrier. It is there so a single address cannot turn the accept endpoint
    into free Argon2 work, and it decays on its own like the login lockout.
    """
    limiter = _invite_limiter(request)
    if limiter is None:
        return
    if limiter.is_blocked(token[:12], client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts on this invitation; try again shortly",
        )


def _record_invite_failure(request: Request, token: str) -> None:
    limiter = _invite_limiter(request)
    if limiter is not None:
        limiter.record_failure(token[:12], client_ip(request))


def _invitation_out(invitation: WorkspaceInvitation, inviter: User | None) -> InvitationOut:
    return InvitationOut(
        id=invitation.id,
        email=invitation.email,
        role=WorkspaceRole(invitation.role),
        status=invite_service.invitation_status(invitation),
        invited_by_user_id=invitation.invited_by_user_id,
        invited_by_name=inviter.display_name if inviter else None,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
        revoked_at=invitation.revoked_at,
        created_at=invitation.created_at,
    )


@invitations_router.get("")
async def list_invitations(ctx: AdminCtx, db: DbSession) -> list[InvitationOut]:
    rows = await invite_service.list_invitations(db, ctx.workspace_id)
    return [_invitation_out(invitation, inviter) for invitation, inviter in rows]


@invitations_router.post("", status_code=201)
async def create_invitation(
    payload: InvitationCreate,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
    settings: SettingsDep,
) -> InvitationCreated:
    created = await invite_service.create_invitation(
        db,
        ctx,
        email=payload.email,
        role=payload.role,
        ttl_days=settings.invitation_ttl_days,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    inviter = ctx.user
    return InvitationCreated(
        invitation=_invitation_out(created.invitation, inviter),
        invite_url=invite_service.invite_url(settings.app_url, created.token),
        token=created.token,
    )


@invitations_router.delete("/{invitation_id}", status_code=204)
async def revoke_invitation(
    invitation_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> None:
    await invite_service.revoke_invitation(
        db, ctx, invitation_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@public_invitations_router.get("/{token}")
async def preview_invitation(token: str, request: Request, db: DbSession) -> InvitationPreview:
    """Public: only the workspace name, the invited address, and the role.

    Nothing else about the workspace is exposed here — whoever holds the link
    is not a member yet.
    """
    _guard_invite_attempt(request, token)
    try:
        invitation, workspace = await invite_service.preview(db, token)
    except HTTPException:
        _record_invite_failure(request, token)
        raise
    return InvitationPreview(
        workspace_name=workspace.name,
        email=invitation.email,
        role=WorkspaceRole(invitation.role),
        expires_at=invitation.expires_at,
    )


@public_invitations_router.post("/{token}/accept", status_code=201)
async def accept_invitation(
    token: str,
    payload: InvitationAccept,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
) -> MeResponse:
    _guard_invite_attempt(request, token)
    try:
        result = await invite_service.accept(
            db,
            token,
            display_name=payload.display_name,
            password=payload.password,
            request_id=req_id(request),
            ip_hash=ip_hash(request),
        )
    except HTTPException:
        _record_invite_failure(request, token)
        raise
    # Accepting is an authentication: the invitee lands signed in rather than
    # being bounced to a login form they have never used.
    session_token = await auth_service.start_session(
        db,
        result.user.id,
        ttl_hours=settings.session_ttl_hours,
        ip_hash=ip_hash(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    set_auth_cookies(response, settings, session_token)
    return await me_response(db, result.user)


# --- API keys ---


def _key_out(record: ApiKey, creator: User | None) -> ApiKeyOut:
    return ApiKeyOut(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=[str(scope) for scope in record.scopes_json],
        role_ceiling=WorkspaceRole(record.role_ceiling),
        created_by_user_id=record.created_by_user_id,
        created_by_name=creator.display_name if creator else None,
        expires_at=record.expires_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
        created_at=record.created_at,
        status=key_service.key_status(record),
    )


def _usage_out(usage: ApiKeyUsage, record: ApiKey | None, actor: User | None) -> ApiKeyUsageOut:
    return ApiKeyUsageOut(
        id=usage.id,
        api_key_id=usage.api_key_id,
        api_key_name=record.name if record else None,
        api_key_prefix=record.prefix if record else None,
        acting_user_id=usage.acting_user_id,
        acting_user_name=actor.display_name if actor else None,
        method=usage.method,
        path=usage.path,
        status_code=usage.status_code,
        created_at=usage.created_at,
    )


@api_keys_router.get("/scopes")
async def scope_catalog(ctx: ViewerCtx) -> ScopeCatalogOut:
    """The taxonomy, annotated with what this caller's role may actually grant.

    The web client renders its scope tree straight from this, so the labels a
    person reads and the strings the API validates can never drift apart.
    """
    allowed = scopes_for_role(ctx.role)
    return ScopeCatalogOut(
        your_role=ctx.role,
        categories=[
            ScopeCategoryOut(
                key=category.key,
                label=category.label,
                description=category.description,
                scopes=[
                    ScopeOut(
                        key=scope.key,
                        category=scope.category,
                        action=scope.action,
                        label=scope.label,
                        description=scope.description,
                        min_role=scope.min_role,
                        available=scope.key in allowed,
                    )
                    for scope in CATEGORY_SCOPES[category.key]
                ],
            )
            for category in CATEGORIES
        ],
    )


@api_keys_router.get("/usage")
async def list_key_usage(
    ctx: ViewerCtx,
    db: DbSession,
    api_key_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=key_service.MAX_USAGE_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiKeyUsagePage:
    rows, total = await key_service.list_usage(
        db, ctx, api_key_id=api_key_id, limit=limit, offset=offset
    )
    return ApiKeyUsagePage(
        items=[_usage_out(usage, record, actor) for usage, record, actor in rows], total=total
    )


@api_keys_router.get("")
async def list_api_keys(ctx: ViewerCtx, db: DbSession) -> list[ApiKeyOut]:
    rows = await key_service.list_keys(db, ctx.workspace_id)
    return [_key_out(record, creator) for record, creator in rows]


@api_keys_router.post("", status_code=201)
async def create_api_key(
    payload: ApiKeyCreate, request: Request, ctx: ViewerCtx, db: DbSession
) -> ApiKeyCreated:
    minted = await key_service.create_key(
        db,
        ctx,
        name=payload.name,
        scopes=payload.scopes,
        expires_in=payload.expires_in,
        expires_unit=payload.expires_unit,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return ApiKeyCreated(api_key=_key_out(minted.record, ctx.user), key=minted.plaintext)


@api_keys_router.delete("/{key_id}", status_code=204)
async def revoke_api_key(key_id: UUID, request: Request, ctx: ViewerCtx, db: DbSession) -> None:
    await key_service.revoke_key(
        db, ctx, key_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
