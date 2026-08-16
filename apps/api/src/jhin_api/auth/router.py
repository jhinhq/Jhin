"""Route handlers for /api/v1/auth. Thin: all logic lives in the service."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from jhin_api.auth import service
from jhin_api.auth.schemas import (
    BootstrapRequest,
    BootstrapStatus,
    LoginRequest,
    MembershipOut,
    MeResponse,
    UserOut,
)
from jhin_api.deps import (
    CurrentAuth,
    DbSession,
    client_ip,
    client_ip_hash,
    get_request_id,
    get_settings_dep,
)
from jhin_api.security.csrf import csrf_protect
from jhin_api.security.tokens import new_csrf_token
from jhin_api.settings import Settings
from jhin_db.models import User
from jhin_domain import WorkspaceRole

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _set_auth_cookies(response: Response, settings: Settings, session_token: str) -> None:
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    # CSRF cookie is intentionally readable by JavaScript (double-submit).
    response.set_cookie(
        settings.csrf_cookie_name,
        new_csrf_token(),
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


async def _me_response(db: DbSession, user: User) -> MeResponse:
    memberships = await service.list_memberships(db, user.id)
    return MeResponse(
        user=UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
        ),
        memberships=[
            MembershipOut(
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_slug=workspace.slug,
                role=WorkspaceRole(membership.role),
            )
            for membership, workspace in memberships
        ],
    )


@router.get("/bootstrap-status")
async def bootstrap_status(db: DbSession) -> BootstrapStatus:
    return BootstrapStatus(needs_bootstrap=await service.needs_bootstrap(db))


@router.post("/bootstrap", status_code=201)
async def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
) -> MeResponse:
    result = await service.bootstrap_owner(
        db,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        workspace_name=payload.workspace_name,
        session_ttl_hours=settings.session_ttl_hours,
        request_id=get_request_id(request),
        ip_hash=client_ip_hash(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, settings, result.session_token)
    return await _me_response(db, result.user)


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
) -> MeResponse:
    result = await service.login(
        db,
        request.app.state.login_limiter,
        email=payload.email,
        password=payload.password,
        session_ttl_hours=settings.session_ttl_hours,
        request_id=get_request_id(request),
        ip=client_ip(request),
        ip_hash=client_ip_hash(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, settings, result.session_token)
    return await _me_response(db, result.user)


@router.post("/logout", status_code=204, dependencies=[Depends(csrf_protect)])
async def logout(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    auth: CurrentAuth,
) -> None:
    await service.logout(
        db,
        session_record=auth.session_record,
        user=auth.user,
        request_id=get_request_id(request),
        ip_hash=client_ip_hash(request),
    )
    _clear_auth_cookies(response, settings)


@router.get("/me")
async def me(db: DbSession, auth: CurrentAuth) -> MeResponse:
    return await _me_response(db, auth.user)
