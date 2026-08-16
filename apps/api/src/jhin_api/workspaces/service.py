"""Workspace and membership business logic (plan 6.1, 6.3, 20.2).

Ownership rules: only owners touch owner-level memberships, and a workspace
can never lose its last owner.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.slugs import slugify, with_suffix
from jhin_db.models import ModelProfile, User, Workspace, WorkspaceMembership
from jhin_domain import WorkspaceRole


async def list_for_user(db: AsyncSession, user_id: UUID) -> list[Workspace]:
    rows = await db.scalars(
        select(Workspace)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(WorkspaceMembership.user_id == user_id)
        .order_by(Workspace.created_at)
    )
    return list(rows)


async def get(db: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


async def create(
    db: AsyncSession,
    *,
    name: str,
    default_timezone: str,
    creator_id: UUID,
    request_id: UUID,
    ip_hash: str,
) -> Workspace:
    slug = slugify(name)
    if await db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
        slug = with_suffix(slug)
    workspace = Workspace(name=name.strip(), slug=slug, default_timezone=default_timezone)
    db.add(workspace)
    await db.flush()
    db.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=creator_id, role=WorkspaceRole.OWNER.value
        )
    )
    audit.record(
        db,
        action="workspace.created",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_id=creator_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": workspace.name, "slug": workspace.slug},
    )
    await db.commit()
    return workspace


async def update(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    changes: dict[str, object],
    request_id: UUID,
    ip_hash: str,
) -> Workspace:
    workspace = await get(db, ctx.workspace_id)
    if "default_model_profile_id" in changes:
        profile_id = changes["default_model_profile_id"]
        exists = await db.scalar(
            select(ModelProfile.id).where(
                ModelProfile.id == profile_id, ModelProfile.workspace_id == ctx.workspace_id
            )
        )
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="default_model_profile_id does not reference a profile in this workspace",
            )
    for field, value in changes.items():
        setattr(workspace, field, value)
    audit.record(
        db,
        action="workspace.updated",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed_fields": sorted(changes)},
    )
    await db.commit()
    return workspace


async def delete(
    db: AsyncSession, ctx: WorkspaceContext, *, request_id: UUID, ip_hash: str
) -> None:
    workspace = await get(db, ctx.workspace_id)
    # The audit row has no FK to workspace, so history survives the delete.
    audit.record(
        db,
        action="workspace.deleted",
        target_type="workspace",
        target_id=workspace.id,
        workspace_id=workspace.id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": workspace.name, "slug": workspace.slug},
    )
    await db.delete(workspace)
    await db.commit()


# --- Members ---


async def list_members(
    db: AsyncSession, workspace_id: UUID
) -> list[tuple[WorkspaceMembership, User]]:
    result = await db.execute(
        select(WorkspaceMembership, User)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(WorkspaceMembership.created_at)
    )
    return [(row[0], row[1]) for row in result.all()]


async def _get_membership(
    db: AsyncSession, workspace_id: UUID, membership_id: UUID
) -> WorkspaceMembership:
    membership = await db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.id == membership_id,
            WorkspaceMembership.workspace_id == workspace_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return membership


async def _owner_count(db: AsyncSession, workspace_id: UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.role == WorkspaceRole.OWNER.value,
        )
    )
    return int(count or 0)


def _require_owner_for_owner_changes(ctx: WorkspaceContext, *roles: WorkspaceRole) -> None:
    """Admins manage members, except anything touching the owner role (20.2)."""
    if WorkspaceRole.OWNER in roles and ctx.role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can grant or modify the owner role",
        )


async def add_member(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    email: str,
    role: WorkspaceRole,
    request_id: UUID,
    ip_hash: str,
) -> tuple[WorkspaceMembership, User]:
    _require_owner_for_owner_changes(ctx, role)
    user = await db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No user account exists with that email",
        )
    existing = await db.scalar(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == ctx.workspace_id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")
    membership = WorkspaceMembership(
        workspace_id=ctx.workspace_id, user_id=user.id, role=role.value
    )
    db.add(membership)
    await db.flush()
    audit.record(
        db,
        action="membership.created",
        target_type="workspace_membership",
        target_id=membership.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"user_id": str(user.id), "role": role.value},
    )
    await db.commit()
    return membership, user


async def update_member_role(
    db: AsyncSession,
    ctx: WorkspaceContext,
    membership_id: UUID,
    *,
    role: WorkspaceRole,
    request_id: UUID,
    ip_hash: str,
) -> tuple[WorkspaceMembership, User]:
    membership = await _get_membership(db, ctx.workspace_id, membership_id)
    current_role = WorkspaceRole(membership.role)
    _require_owner_for_owner_changes(ctx, role, current_role)
    if (
        current_role == WorkspaceRole.OWNER
        and role != WorkspaceRole.OWNER
        and await _owner_count(db, ctx.workspace_id) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace must keep at least one owner",
        )
    membership.role = role.value
    audit.record(
        db,
        action="membership.updated",
        target_type="workspace_membership",
        target_id=membership.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"from_role": current_role.value, "to_role": role.value},
    )
    await db.commit()
    user = await db.get(User, membership.user_id)
    assert user is not None  # FK guarantees the user row exists
    return membership, user


async def remove_member(
    db: AsyncSession,
    ctx: WorkspaceContext,
    membership_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    membership = await _get_membership(db, ctx.workspace_id, membership_id)
    current_role = WorkspaceRole(membership.role)
    _require_owner_for_owner_changes(ctx, current_role)
    if current_role == WorkspaceRole.OWNER and await _owner_count(db, ctx.workspace_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace must keep at least one owner",
        )
    audit.record(
        db,
        action="membership.deleted",
        target_type="workspace_membership",
        target_id=membership.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"user_id": str(membership.user_id), "role": current_role.value},
    )
    await db.delete(membership)
    await db.commit()
