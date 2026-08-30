"""Shared FastAPI dependencies: DB sessions, auth context, workspace RBAC.

Workspace isolation (plan 20.2, 48.4): every workspace-scoped route resolves
membership through ``require_workspace_role`` and every service query filters
by ``workspace_id``. Non-members receive 404 so workspace existence is not
leaked; members with an insufficient role receive 403.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Annotated, Any
from uuid import UUID

import httpx
import nats
from fastapi import Depends, HTTPException, Request, status
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.service import RPCError

from jhin_api.access import keys as api_keys
from jhin_api.access.keys import ApiKeyPrincipal
from jhin_api.access.route_scopes import required_scope
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token
from jhin_api.settings import Settings
from jhin_api.temporal import TemporalClientProvider
from jhin_db.models import User, UserSession, WorkspaceMembership
from jhin_domain import UserStatus, WorkspaceRole, role_satisfies, scopes_for_role
from jhin_observability import ObservabilityRuntime
from jhin_secrets import SecretCrypto


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_observability_runtime(request: Request) -> ObservabilityRuntime:
    runtime = getattr(request.app.state, "observability", None)
    if not isinstance(runtime, ObservabilityRuntime):
        raise RuntimeError("API observability runtime is unavailable")
    return runtime


ObservabilityRuntimeDep = Annotated[
    ObservabilityRuntime,
    Depends(get_observability_runtime),
]


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_request_id(request: Request) -> UUID:
    request_id: UUID = request.state.request_id
    return request_id


def _is_trusted_proxy(candidate: str, networks: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def client_ip(request: Request) -> str:
    """Best-effort originating address, honouring only trusted proxies.

    The API normally sits behind the Next.js rewrite proxy, so ``request.client``
    is the proxy for every request. Without ``TRUSTED_PROXY_CIDRS`` the per-IP
    login lockout would treat the whole internet as one address and could lock
    every user out at once, so the forwarded chain is consulted — but *only*
    when the immediate peer is itself a configured trusted proxy, and only back
    to the first hop that is not one. An untrusted client cannot forge its own
    address by sending ``X-Forwarded-For``.
    """
    peer = request.client.host if request.client else "unknown"
    settings: Settings | None = getattr(request.app.state, "settings", None)
    networks = settings.trusted_proxy_networks if settings is not None else ()
    if not networks or not _is_trusted_proxy(peer, networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for raw in reversed(forwarded.split(",")):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            ip_address(candidate)
        except ValueError:
            continue
        if not _is_trusted_proxy(candidate, networks):
            return candidate
    return peer


def client_ip_hash(request: Request) -> str:
    """Audit rows store a hash, never the raw address (plan 6.17)."""
    return hashlib.sha256(client_ip(request).encode()).hexdigest()


@dataclass(frozen=True)
class AuthContext:
    user: User
    session_record: UserSession


@dataclass(frozen=True)
class WorkspaceContext:
    user: User
    workspace_id: UUID
    role: WorkspaceRole
    # Set only when the caller authenticated with an API key rather than a
    # browser session. ``role`` is already capped by the key's ceiling, and
    # ``api_key.scopes`` is the effective (already intersected) scope set.
    api_key: ApiKeyPrincipal | None = None


@dataclass(frozen=True)
class Principal:
    """Whoever is making this request: a browser session or an API key."""

    user: User
    session_record: UserSession | None = None
    api_key: ApiKeyPrincipal | None = None


DbSession = Annotated[AsyncSession, Depends(get_db)]


def _as_utc(value: datetime) -> datetime:
    """SQLite (unit tests) hands back naive datetimes; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def normalize_user_agent(raw: str | None) -> str | None:
    """Same truncation the session row uses, so comparisons are apples to apples."""
    return (raw or "")[:400] or None


async def get_current_auth(
    request: Request,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> AuthContext:
    token = request.cookies.get(settings.session_cookie_name)
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
    )
    if not token:
        raise unauthorized

    now = datetime.now(UTC)
    record = await db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    # Absolute expiry: a session dies at `expires_at` however actively it is used.
    if record is None or record.revoked_at is not None or _as_utc(record.expires_at) <= now:
        raise unauthorized

    async def revoke_and_reject() -> HTTPException:
        record.revoked_at = now
        await db.commit()
        return unauthorized

    # Idle expiry: an abandoned session stops being a credential well before
    # the absolute lifetime runs out.
    last_seen = _as_utc(record.last_used_at or record.created_at)
    if now - last_seen > timedelta(hours=settings.session_idle_timeout_hours):
        raise await revoke_and_reject()

    # Client binding: a cookie replayed from a different client is treated as
    # theft and kills the session. The address is deliberately *not* part of
    # the binding, so roaming between networks does not log people out.
    if settings.session_bind_user_agent and record.user_agent is not None:
        presented = normalize_user_agent(request.headers.get("user-agent"))
        if presented != record.user_agent:
            raise await revoke_and_reject()

    user = await db.get(User, record.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise unauthorized

    record.last_used_at = now
    await db.commit()
    return AuthContext(user=user, session_record=record)


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


async def get_current_auth_optional(
    request: Request,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> AuthContext | None:
    """:func:`get_current_auth`, but ``None`` instead of a 401.

    For the one route that a *provider* redirects a browser to, where a raw
    ``401 {"detail": "Not authenticated"}`` would be the last thing a person
    sees after a slow consent screen outlived their session. Every check in
    :func:`get_current_auth` still runs and still has the same effect —
    including revoking a session that fails client binding — so this grants
    nothing; it only lets the caller render the failure as a page.

    Not a general-purpose dependency. A route that needs a user must use
    :data:`CurrentAuth`; this one exists so the callback can redirect.
    """
    try:
        return await get_current_auth(request, db, settings)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


OptionalAuth = Annotated[AuthContext | None, Depends(get_current_auth_optional)]


@dataclass(frozen=True)
class ApiKeyUsageRecord:
    """Stashed on ``request.state`` for :class:`ApiKeyUsageMiddleware`."""

    api_key_id: UUID
    workspace_id: UUID
    acting_user_id: UUID
    ip_hash: str


async def get_current_principal(
    request: Request,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> Principal:
    """Resolve the caller from an API key if one is presented, else the cookie.

    API keys are checked first and are terminal: presenting a bad key never
    silently falls back to whatever cookie the same client happens to hold.
    """
    raw = api_keys.bearer_token(request.headers.get("authorization"))
    if raw is None:
        auth = await get_current_auth(request, db, settings)
        return Principal(user=auth.user, session_record=auth.session_record)

    limiter: LoginRateLimiter | None = getattr(request.app.state, "api_key_limiter", None)
    ip = client_ip(request)
    parsed = api_keys.parse_key(raw)
    limiter_key = parsed[0] if parsed else "malformed"
    if limiter is not None and limiter.is_blocked(limiter_key, ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many API key attempts; try again later",
        )
    try:
        principal, user = await api_keys.authenticate(db, raw)
    except api_keys.ApiKeyAuthError as exc:
        if limiter is not None and exc.rate_limited:
            limiter.record_failure(limiter_key, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message) from exc
    if limiter is not None:
        limiter.reset(limiter_key, ip)
    # Recorded here so the usage middleware can log the call even when the
    # scope or role check below rejects it.
    request.state.api_key_usage = ApiKeyUsageRecord(
        api_key_id=principal.id,
        workspace_id=principal.workspace_id,
        acting_user_id=user.id,
        ip_hash=client_ip_hash(request),
    )
    return Principal(user=user, api_key=principal)


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else request.url.path


def _enforce_api_key(request: Request, principal: ApiKeyPrincipal, workspace_id: UUID) -> None:
    """The central scope gate: one place, every workspace-scoped route.

    Endpoints never check scopes themselves, so a new endpoint cannot forget
    to. A route with no entry in ``ROUTE_SCOPES`` is unreachable by key.
    """
    if principal.workspace_id != workspace_id:
        # Same shape as a non-member: never confirm the workspace exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    needed = required_scope(request.method, _route_template(request))
    if needed is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is not available to API keys",
        )
    if needed not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is missing the '{needed}' scope",
        )


def require_workspace_role(
    required: WorkspaceRole,
) -> Callable[..., Coroutine[Any, Any, WorkspaceContext]]:
    """Dependency factory: caller must be a workspace member with >= role.

    For API keys the effective role is ``min(membership role, key ceiling)``
    and the route's scope must be within the key's effective scopes.
    """

    async def dependency(
        workspace_id: UUID,
        request: Request,
        principal: CurrentPrincipal,
        db: DbSession,
    ) -> WorkspaceContext:
        membership = await db.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == principal.user.id,
            )
        )
        if membership is None:
            # 404, not 403: non-members must not learn the workspace exists.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        role = WorkspaceRole(membership.role)
        key = principal.api_key
        if key is not None:
            # A key never outranks its ceiling, even if its creator was
            # promoted afterwards — and never outranks the creator's role
            # today either, so a demotion takes effect immediately.
            if not role_satisfies(role, key.role_ceiling):
                key = replace(key, role_ceiling=role, scopes=key.scopes & scopes_for_role(role))
            _enforce_api_key(request, key, workspace_id)
            role = key.role_ceiling
        if not role_satisfies(role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires workspace role '{required.value}' or higher",
            )
        return WorkspaceContext(
            user=principal.user, workspace_id=workspace_id, role=role, api_key=key
        )

    return dependency


ViewerCtx = Annotated[WorkspaceContext, Depends(require_workspace_role(WorkspaceRole.VIEWER))]
MemberCtx = Annotated[WorkspaceContext, Depends(require_workspace_role(WorkspaceRole.MEMBER))]
AdminCtx = Annotated[WorkspaceContext, Depends(require_workspace_role(WorkspaceRole.ADMIN))]
OwnerCtx = Annotated[WorkspaceContext, Depends(require_workspace_role(WorkspaceRole.OWNER))]


def get_secret_crypto(request: Request) -> SecretCrypto:
    """Envelope crypto bound to the master key loaded at startup.

    503 (not 500) when the key is absent: the operator can fix this without a
    code change by mounting the key file and restarting.
    """
    crypto: SecretCrypto | None = request.app.state.secret_crypto
    if crypto is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Secret encryption is unavailable: no master key configured "
            "(set MASTER_KEY_FILE; see `make master-key`)",
        )
    return crypto


SecretCryptoDep = Annotated[SecretCrypto, Depends(get_secret_crypto)]


#: Every outbound OAuth call is bounded and redirect-free. Redirects are
#: refused rather than followed because a 302 on a metadata or token endpoint
#: is a way to move a request to a host the SSRF policy never approved, and
#: the timeout is short because a provider that cannot answer in ten seconds
#: is a provider the person waiting at a consent screen has already given up
#: on.
OAUTH_HTTP_TIMEOUT_SECONDS = 10.0


async def get_oauth_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """A short-lived HTTP client for one request's OAuth work."""
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(OAUTH_HTTP_TIMEOUT_SECONDS),
    ) as client:
        yield client


OAuthHttpClientDep = Annotated[httpx.AsyncClient, Depends(get_oauth_http_client)]


async def get_temporal_client(request: Request) -> TemporalClient:
    """Process-wide Temporal client, connected lazily on first use.

    Lazy so the API can boot (and serve everything except task execution)
    while Temporal is still starting. 503 tells the caller to retry.
    """
    provider: TemporalClientProvider = request.app.state.temporal_provider
    try:
        return await provider.get()
    except (RPCError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task orchestration is unavailable (cannot reach Temporal)",
        ) from exc


TemporalDep = Annotated[TemporalClient, Depends(get_temporal_client)]


async def get_jetstream(request: Request) -> JetStreamContext:
    """Process-wide NATS JetStream context, connected lazily on first use
    (same pattern as Temporal). Used by the webhook ingress path."""
    app = request.app
    cached: NatsClient | None = app.state.nats_client
    if cached is not None and not cached.is_closed:
        return cached.jetstream()
    async with app.state.nats_connect_lock:
        cached = app.state.nats_client
        if cached is not None and not cached.is_closed:
            return cached.jetstream()
        settings: Settings = app.state.settings
        try:
            client = await nats.connect(settings.nats_url, connect_timeout=3)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Event backbone is unavailable (cannot reach NATS)",
            ) from exc
        try:
            app.state.nats_client = client
        except BaseException:
            with suppress(BaseException):
                await client.close()
            raise
        return client.jetstream()


JetStreamDep = Annotated[JetStreamContext, Depends(get_jetstream)]
