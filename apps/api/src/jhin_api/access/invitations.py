"""Workspace invitations: create, list, revoke, preview, accept.

Invitations, not admin-set passwords: an admin who types a new colleague's
password has, by definition, seen it. An invite link is single-use, expires,
and lets the invitee choose their own credential — nobody else ever holds it.

There is no email sender in Jhin and this module deliberately does not add
one. The plaintext token is returned to the inviting admin exactly once, as a
URL to pass along out of band.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio.to_thread
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.schemas import InvitationStatus
from jhin_api.audit import service as audit
from jhin_api.auth.service import enforce_password_policy
from jhin_api.deps import WorkspaceContext
from jhin_api.security.passwords import hash_password
from jhin_api.security.tokens import hash_token, new_session_token
from jhin_api.workspaces.service import require_authority_over
from jhin_db.models import User, Workspace, WorkspaceInvitation, WorkspaceMembership
from jhin_domain import ActorType, UserStatus, WorkspaceRole

# One wording for every bad-token outcome so a caller cannot distinguish
# "never existed" from "already used" from "expired".
_INVALID = "This invitation link is not valid any more"


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: WorkspaceInvitation
    token: str


@dataclass(frozen=True)
class AcceptedInvitation:
    user: User
    workspace: Workspace
    role: WorkspaceRole


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def invitation_status(
    invitation: WorkspaceInvitation, *, now: datetime | None = None
) -> InvitationStatus:
    moment = now or datetime.now(UTC)
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if _as_utc(invitation.expires_at) <= moment:
        return "expired"
    return "pending"


def invite_url(app_url: str, token: str) -> str:
    return f"{app_url.rstrip('/')}/invite/{token}"


async def list_invitations(
    db: AsyncSession, workspace_id: UUID
) -> list[tuple[WorkspaceInvitation, User | None]]:
    rows = await db.execute(
        select(WorkspaceInvitation, User)
        .outerjoin(User, User.id == WorkspaceInvitation.invited_by_user_id)
        .where(WorkspaceInvitation.workspace_id == workspace_id)
        .order_by(WorkspaceInvitation.created_at.desc())
    )
    return [(row[0], row[1]) for row in rows.all()]


async def create_invitation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    email: str,
    role: WorkspaceRole,
    ttl_days: int,
    request_id: UUID,
    ip_hash: str,
) -> CreatedInvitation:
    require_authority_over(ctx, role)
    normalized = email.strip().lower()

    existing_user = await db.scalar(select(User).where(User.email == normalized))
    if existing_user is not None:
        already_member = await db.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == ctx.workspace_id,
                WorkspaceMembership.user_id == existing_user.id,
            )
        )
        if already_member:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That person is already a member of this workspace",
            )

    now = datetime.now(UTC)
    # Re-inviting the same address supersedes any outstanding link, so an
    # unshared or lost invitation cannot linger as a second live credential.
    outstanding = await db.scalars(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == ctx.workspace_id,
            WorkspaceInvitation.email == normalized,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
        )
    )
    for stale in outstanding:
        stale.revoked_at = now

    token = new_session_token()
    invitation = WorkspaceInvitation(
        workspace_id=ctx.workspace_id,
        email=normalized,
        role=role.value,
        token_hash=hash_token(token),
        invited_by_user_id=ctx.user.id,
        expires_at=now + timedelta(days=ttl_days),
    )
    db.add(invitation)
    await db.flush()
    audit.record(
        db,
        action="invitation.created",
        target_type="workspace_invitation",
        target_id=invitation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"email": normalized, "role": role.value},
    )
    await db.commit()
    return CreatedInvitation(invitation=invitation, token=token)


async def revoke_invitation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    invitation_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    invitation = await db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == ctx.workspace_id,
        )
    )
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    require_authority_over(ctx, WorkspaceRole(invitation.role))
    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That invitation has already been accepted",
        )
    invitation.revoked_at = datetime.now(UTC)
    audit.record(
        db,
        action="invitation.revoked",
        target_type="workspace_invitation",
        target_id=invitation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"email": invitation.email},
    )
    await db.commit()


async def _usable_invitation(db: AsyncSession, token: str) -> WorkspaceInvitation:
    """Look the token up by hash: one indexed equality, no comparison ladder.

    Hashing first means the lookup does the same work for a wrong token as a
    right one, and the database never holds anything replayable.
    """
    invitation = await db.scalar(
        select(WorkspaceInvitation).where(WorkspaceInvitation.token_hash == hash_token(token))
    )
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INVALID)
    if invitation is None or invitation_status(invitation) != "pending":
        raise not_found
    return invitation


async def preview(db: AsyncSession, token: str) -> tuple[WorkspaceInvitation, Workspace]:
    invitation = await _usable_invitation(db, token)
    workspace = await db.get(Workspace, invitation.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INVALID)
    return invitation, workspace


async def accept(
    db: AsyncSession,
    token: str,
    *,
    display_name: str,
    password: str,
    request_id: UUID,
    ip_hash: str,
) -> AcceptedInvitation:
    """Create the account and the membership atomically, then hand back both.

    Single-use is enforced by stamping ``accepted_at`` inside the same
    transaction that creates the membership: a replay of the same link finds
    the invitation no longer ``pending``.
    """
    invitation = await _usable_invitation(db, token)
    workspace = await db.get(Workspace, invitation.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_INVALID)

    role = WorkspaceRole(invitation.role)
    existing = await db.scalar(select(User).where(User.email == invitation.email))
    now = datetime.now(UTC)

    if existing is None:
        # Same policy, same wording as bootstrap and password change.
        enforce_password_policy(password, email=invitation.email)
        password_hash = await anyio.to_thread.run_sync(hash_password, password)
        user = User(
            email=invitation.email,
            display_name=display_name.strip(),
            password_hash=password_hash,
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.flush()
    else:
        # The address already has an account (invited into a second workspace).
        # Joining must not be a password-reset primitive, so the submitted
        # password is ignored entirely and the existing credential stands.
        user = existing

    already = await db.scalar(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if not already:
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role.value))
    invitation.accepted_at = now
    invitation.accepted_user_id = user.id

    audit.record(
        db,
        action="invitation.accepted",
        target_type="workspace_invitation",
        target_id=invitation.id,
        workspace_id=workspace.id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"role": role.value, "new_account": existing is None},
    )
    audit.record(
        db,
        action="membership.created",
        target_type="workspace_membership",
        target_id=invitation.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"user_id": str(user.id), "role": role.value, "via": "invitation"},
    )
    await db.commit()
    return AcceptedInvitation(user=user, workspace=workspace, role=role)
