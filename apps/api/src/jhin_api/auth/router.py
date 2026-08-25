"""Route handlers for /api/v1/auth. Thin: all logic lives in the service."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response

from jhin_api.auth import service
from jhin_api.auth.schemas import (
    BootstrapRequest,
    BootstrapStatus,
    ChangePasswordRequest,
    LoginRequest,
    MembershipOut,
    MeResponse,
    SessionsRevokedResponse,
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
from jhin_api.security.tokens import csrf_token_for_session
from jhin_api.settings import Settings
from jhin_db.models import User
from jhin_domain import WorkspaceRole

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _samesite(settings: Settings) -> Literal["lax", "strict", "none"]:
    value = settings.session_cookie_samesite.lower()
    if value == "strict":
        return "strict"
    if value == "none":
        return "none"
    return "lax"


def _set_auth_cookies(response: Response, settings: Settings, session_token: str) -> None:
    max_age = settings.session_ttl_hours * 3600
    same_site = _samesite(settings)
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=max_age,
        httponly=True,
        samesite=same_site,
        secure=settings.cookie_secure,
        path="/",
    )
    # CSRF cookie is intentionally readable by JavaScript (double-submit) but
    # is derived from the session token, so it is worthless on any other
    # session — see jhin_api.security.tokens.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token_for_session(session_token),
        max_age=max_age,
        httponly=False,
        samesite=same_site,
        secure=settings.cookie_secure,
        path="/",
    )


def _refresh_csrf_cookie(request: Request, response: Response, settings: Settings) -> None:
    """Re-issue the session-bound CSRF cookie on a read.

    Keeps a client whose CSRF cookie was dropped, expired, or overwritten from
    getting stuck: the next page load repairs it instead of failing every
    mutation with a 403 the user cannot clear (logout is CSRF-protected too).
    """
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        return
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token_for_session(session_token),
        max_age=settings.session_ttl_hours * 3600,
        httponly=False,
        samesite=_samesite(settings),
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    # Deletion only takes effect when the attributes match the cookie that was
    # set, so the flags are repeated here rather than left to defaults.
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            samesite=_samesite(settings),
            secure=settings.cookie_secure,
        )


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


# Public aliases for the invitation accept flow (``jhin_api.access.router``),
# which signs a brand-new account in and must use this exact cookie policy and
# response shape rather than a second copy of them.
set_auth_cookies = _set_auth_cookies
me_response = _me_response


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
async def me(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    auth: CurrentAuth,
) -> MeResponse:
    _refresh_csrf_cookie(request, response, settings)
    return await _me_response(db, auth.user)


@router.post("/logout-all", status_code=200, dependencies=[Depends(csrf_protect)])
async def logout_all(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    auth: CurrentAuth,
) -> SessionsRevokedResponse:
    """Sign out of every browser, everywhere — including this one."""
    revoked = await service.logout_everywhere(
        db,
        user=auth.user,
        request_id=get_request_id(request),
        ip_hash=client_ip_hash(request),
    )
    _clear_auth_cookies(response, settings)
    return SessionsRevokedResponse(revoked_sessions=revoked)


@router.post("/password", dependencies=[Depends(csrf_protect)])
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    auth: CurrentAuth,
) -> MeResponse:
    """Change the password; every other session dies and this one is re-seated."""
    token = await service.change_password(
        db,
        user=auth.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        session_ttl_hours=settings.session_ttl_hours,
        request_id=get_request_id(request),
        ip_hash=client_ip_hash(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, settings, token)
    return await _me_response(db, auth.user)
