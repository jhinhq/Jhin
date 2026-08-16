"""Authentication business logic: owner bootstrap, login, logout, sessions.

Route handlers stay thin (plan 47); everything stateful happens here.
Passwords are hashed with Argon2id in a worker thread and are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio.to_thread
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.security.passwords import hash_password, verify_password
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token, new_session_token
from jhin_api.slugs import slugify
from jhin_db.models import User, UserSession, Workspace, WorkspaceMembership
from jhin_domain import ActorType, UserStatus, WorkspaceRole

# Verified when the email is unknown so response timing does not reveal
# whether an account exists.
_DUMMY_HASH = hash_password("jhin-timing-equalizer")


@dataclass(frozen=True)
class LoginResult:
    user: User
    session_token: str


async def needs_bootstrap(db: AsyncSession) -> bool:
    """True while no user exists; the bootstrap endpoint disables itself after."""
    count = await db.scalar(select(func.count()).select_from(User))
    return (count or 0) == 0


async def _create_session(
    db: AsyncSession,
    user_id: UUID,
    *,
    ttl_hours: int,
    ip_hash: str | None,
    user_agent: str | None,
) -> str:
    token = new_session_token()
    db.add(
        UserSession(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            ip_hash=ip_hash,
            user_agent=(user_agent or "")[:400] or None,
        )
    )
    return token


async def bootstrap_owner(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    workspace_name: str,
    session_ttl_hours: int,
    request_id: UUID,
    ip_hash: str,
    user_agent: str | None,
) -> LoginResult:
    """First-run flow (plan 43 steps 1-2): create owner user + workspace.

    Refuses to run once any user exists, so it cannot be used to take over an
    installed instance.
    """
    if not await needs_bootstrap(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap is disabled: an owner account already exists",
        )

    password_hash = await anyio.to_thread.run_sync(hash_password, password)
    user = User(
        email=email.strip().lower(),
        display_name=display_name.strip(),
        password_hash=password_hash,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()

    workspace = Workspace(name=workspace_name.strip(), slug=slugify(workspace_name))
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER.value
        )
    )

    audit.record(
        db,
        action="auth.owner_bootstrapped",
        target_type="user",
        target_id=user.id,
        workspace_id=workspace.id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    audit.record(
        db,
        action="workspace.created",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": workspace.name, "slug": workspace.slug},
    )

    token = await _create_session(
        db, user.id, ttl_hours=session_ttl_hours, ip_hash=ip_hash, user_agent=user_agent
    )
    audit.record(
        db,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await db.commit()
    return LoginResult(user=user, session_token=token)


async def login(
    db: AsyncSession,
    limiter: LoginRateLimiter,
    *,
    email: str,
    password: str,
    session_ttl_hours: int,
    request_id: UUID,
    ip: str,
    ip_hash: str,
    user_agent: str | None,
) -> LoginResult:
    email = email.strip().lower()
    if limiter.is_blocked(email, ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts; try again later",
        )

    user = await db.scalar(select(User).where(User.email == email))
    candidate_hash = user.password_hash if user else _DUMMY_HASH
    valid = await anyio.to_thread.run_sync(verify_password, candidate_hash, password)

    if user is None or not valid or user.status != UserStatus.ACTIVE.value:
        limiter.record_failure(email, ip)
        audit.record(
            db,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id if user else None,
            workspace_id=None,
            actor_type=ActorType.SYSTEM,
            request_id=request_id,
            ip_hash=ip_hash,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )

    limiter.reset(email, ip)
    token = await _create_session(
        db, user.id, ttl_hours=session_ttl_hours, ip_hash=ip_hash, user_agent=user_agent
    )
    audit.record(
        db,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        workspace_id=None,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await db.commit()
    return LoginResult(user=user, session_token=token)


async def logout(
    db: AsyncSession,
    *,
    session_record: UserSession,
    user: User,
    request_id: UUID,
    ip_hash: str,
) -> None:
    """Revoke the current session server-side (plan 20.1: session revocation)."""
    session_record.revoked_at = datetime.now(UTC)
    audit.record(
        db,
        action="auth.logout",
        target_type="user",
        target_id=user.id,
        workspace_id=None,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await db.commit()


async def list_memberships(
    db: AsyncSession, user_id: UUID
) -> list[tuple[WorkspaceMembership, Workspace]]:
    result = await db.execute(
        select(WorkspaceMembership, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(WorkspaceMembership.user_id == user_id)
        .order_by(Workspace.created_at)
    )
    return [(row[0], row[1]) for row in result.all()]
