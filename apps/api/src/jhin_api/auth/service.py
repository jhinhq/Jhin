"""Authentication business logic: owner bootstrap, login, logout, sessions.

Route handlers stay thin (plan 47); everything stateful happens here.
Passwords are hashed with Argon2id in a worker thread and are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import anyio.to_thread
from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token, new_session_token
from jhin_api.skills import service as skills_service
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


def enforce_password_policy(password: str, *, email: str) -> None:
    """Translate a policy failure into a 422 the UI can show verbatim."""
    try:
        validate_password_strength(password, email=email)
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


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
    """Mint a brand-new session row and return its plaintext token.

    Every authentication mints a fresh token, so a token planted in the
    victim's browser before login is never the token they end up authenticated
    with — session fixation has no purchase here.
    """
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


async def start_session(
    db: AsyncSession,
    user_id: UUID,
    *,
    ttl_hours: int,
    ip_hash: str | None,
    user_agent: str | None,
) -> str:
    """Seat a user in a fresh session from another authenticating flow.

    Used by invitation accept (``jhin_api.access.router``), which authenticates
    a brand-new account and must mint its session with exactly the policy above
    rather than a second, drifting copy of it.
    """
    return await _create_session(
        db, user_id, ttl_hours=ttl_hours, ip_hash=ip_hash, user_agent=user_agent
    )


async def revoke_all_sessions(
    db: AsyncSession, user_id: UUID, *, except_session_id: UUID | None = None
) -> int:
    """Revoke every live session for a user; returns how many were revoked."""
    now = datetime.now(UTC)
    statement = (
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .values(revoked_at=now)
    )
    if except_session_id is not None:
        statement = statement.where(UserSession.id != except_session_id)
    result = await db.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


async def create_owner_and_workspace(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    workspace_name: str,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    metadata: dict[str, Any] | None = None,
) -> tuple[User, Workspace]:
    """Stage the first owner, their workspace, and its starter skills.

    Shared by the ``/setup`` page and by ``jhin-admin owner create`` so the two
    first-run paths cannot drift: whichever one an install uses, the same rows
    exist afterwards. Stages only — the caller commits, and decides whether the
    run also seats a session (a console one must not: nobody is holding the
    token it would mint).

    Refuses to run once any user exists, so it cannot be used to take over an
    installed instance.
    """
    if not await needs_bootstrap(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap is disabled: an owner account already exists",
        )

    enforce_password_policy(password, email=email)
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
        actor_type=actor_type,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata=metadata,
    )
    audit.record(
        db,
        action="workspace.created",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_type=actor_type,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": workspace.name, "slug": workspace.slug, **(metadata or {})},
    )
    # Every new workspace starts with the five starter skills already
    # installed and enabled (docs/architecture/skills.md).
    await skills_service.install_builtins_for_new_workspace(
        db,
        workspace.id,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    return user, workspace


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

    The browser half of :func:`create_owner_and_workspace`: the operator who
    filled in the form is signed in on the spot, so the response can seat them
    without a second trip through the login form.
    """
    user, workspace = await create_owner_and_workspace(
        db,
        email=email,
        password=password,
        display_name=display_name,
        workspace_name=workspace_name,
        request_id=request_id,
        ip_hash=ip_hash,
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
    decision = limiter.check(email, ip)
    if decision.blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many failed sign-in attempts. This lock clears itself in "
                f"about {max(1, decision.retry_after_seconds // 60)} minute(s) — "
                "no administrator action is required."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
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
    # Free parameter upgrade: if argon2-cffi has raised its defaults since this
    # hash was written, re-hash now while the plaintext is briefly in hand.
    if needs_rehash(user.password_hash):
        user.password_hash = await anyio.to_thread.run_sync(hash_password, password)
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


async def logout_everywhere(
    db: AsyncSession,
    *,
    user: User,
    request_id: UUID,
    ip_hash: str,
) -> int:
    """Revoke every session this user holds, including the calling one.

    The escape hatch for "my laptop was stolen" and for anyone who suspects a
    cookie has been copied.
    """
    revoked = await revoke_all_sessions(db, user.id)
    audit.record(
        db,
        action="auth.sessions_revoked",
        target_type="user",
        target_id=user.id,
        workspace_id=None,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"revoked_sessions": revoked},
    )
    await db.commit()
    return revoked


async def change_password(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    session_ttl_hours: int,
    request_id: UUID,
    ip_hash: str,
    user_agent: str | None,
) -> str:
    """Rotate the password, then every session, and re-seat the caller.

    A password change is the standard response to "I think someone has my
    credentials", so it must invalidate every session that existed under the
    old password — otherwise the attacker keeps their cookie and the victim
    has changed nothing that matters.
    """
    valid = await anyio.to_thread.run_sync(verify_password, user.password_hash, current_password)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect"
        )
    enforce_password_policy(new_password, email=user.email)
    if new_password == current_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="New password must differ from the current password",
        )

    user.password_hash = await anyio.to_thread.run_sync(hash_password, new_password)
    revoked = await revoke_all_sessions(db, user.id)
    token = await _create_session(
        db, user.id, ttl_hours=session_ttl_hours, ip_hash=ip_hash, user_agent=user_agent
    )
    audit.record(
        db,
        action="auth.password_changed",
        target_type="user",
        target_id=user.id,
        workspace_id=None,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"revoked_sessions": revoked},
    )
    await db.commit()
    return token


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
