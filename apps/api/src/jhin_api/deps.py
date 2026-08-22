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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import nats
from fastapi import Depends, HTTPException, Request, status
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.service import RPCError

from jhin_api.security.tokens import hash_token
from jhin_api.settings import Settings
from jhin_api.temporal import TemporalClientProvider
from jhin_db.models import User, UserSession, WorkspaceMembership
from jhin_domain import UserStatus, WorkspaceRole, role_satisfies
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


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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


DbSession = Annotated[AsyncSession, Depends(get_db)]


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
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        raise unauthorized

    user = await db.get(User, record.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise unauthorized

    record.last_used_at = now
    await db.commit()
    return AuthContext(user=user, session_record=record)


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


def require_workspace_role(
    required: WorkspaceRole,
) -> Callable[..., Coroutine[Any, Any, WorkspaceContext]]:
    """Dependency factory: caller must be a workspace member with >= role."""

    async def dependency(
        workspace_id: UUID,
        auth: CurrentAuth,
        db: DbSession,
    ) -> WorkspaceContext:
        membership = await db.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == auth.user.id,
            )
        )
        if membership is None:
            # 404, not 403: non-members must not learn the workspace exists.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        role = WorkspaceRole(membership.role)
        if not role_satisfies(role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires workspace role '{required.value}' or higher",
            )
        return WorkspaceContext(user=auth.user, workspace_id=workspace_id, role=role)

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
