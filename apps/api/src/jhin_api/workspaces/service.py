"""Workspace and membership business logic (plan 6.1, 6.3, 20.2).

Ownership rules: only owners touch owner-level memberships, and a workspace
can never lose its last owner.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.skills import service as skills_service
from jhin_api.slugs import slugify, with_suffix
from jhin_db.models import (
    Agent,
    ApiKey,
    Connection,
    Conversation,
    MemoryRecord,
    Message,
    ModelProfile,
    Secret,
    Skill,
    Task,
    Team,
    Trigger,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import ActorType, WorkspaceRole


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
    ip_hash: str | None,
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
    # Every new workspace starts with the five starter skills already
    # installed and enabled (docs/architecture/skills.md) — staged in this
    # same transaction, not a separate follow-up call.
    await skills_service.install_builtins_for_new_workspace(
        db,
        workspace.id,
        actor_id=creator_id,
        request_id=request_id,
        ip_hash=ip_hash,
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
    audit_fields = sorted(changes)
    incoming_settings = changes.pop("settings", None)
    if isinstance(incoming_settings, dict):
        # Validated sections merge over existing settings_json so unrelated
        # keys survive (delegation depth guard + workspace concurrency,
        # plan 7.5 / 30).
        workspace.settings_json = {**workspace.settings_json, **incoming_settings}
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
        metadata={"changed_fields": audit_fields},
    )
    await db.commit()
    return workspace


#: The categories the deletion confirmation counts, in the order it shows
#: them. Each entry is a table whose ``workspace_id`` foreign key cascades, so
#: every row counted here really does disappear with the workspace. The list is
#: deliberately not exhaustive — runs, tool calls, approvals and the rest go
#: too — but naming *those* would turn a warning into a schema dump.
_DELETION_COUNTS: tuple[tuple[str, type[Any]], ...] = (
    ("agents", Agent),
    ("teams", Team),
    ("tasks", Task),
    ("conversations", Conversation),
    ("messages", Message),
    ("memories", MemoryRecord),
    ("skills", Skill),
    ("connections", Connection),
    ("triggers", Trigger),
    ("api_keys", ApiKey),
    ("secrets", Secret),
    ("members", WorkspaceMembership),
)


async def deletion_summary(db: AsyncSession, workspace_id: UUID) -> dict[str, int]:
    """Count, in one round trip, what deleting this workspace would destroy.

    Counted rather than estimated: a confirmation that says "12 agents" and is
    wrong is worse than one that says nothing, and the caller is about to make
    an irreversible decision on the strength of these numbers.
    """
    columns = [
        select(func.count())
        .select_from(model)
        .where(model.workspace_id == workspace_id)
        .scalar_subquery()
        .label(name)
        for name, model in _DELETION_COUNTS
    ]
    row = (await db.execute(select(*columns))).one()
    return {name: int(value or 0) for (name, _), value in zip(_DELETION_COUNTS, row, strict=True)}


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


def require_authority_over(ctx: WorkspaceContext, *roles: WorkspaceRole) -> None:
    """May ``ctx`` hand out these roles? Only an owner may hand out ownership.

    Admins may invite and promote up to admin — a workspace that needs a
    second operator should not need the owner awake to get one. Ownership is
    different: it carries workspace deletion and the power to unseat the other
    owners, so it may only ever be granted by someone who already holds it
    (docs/architecture/rbac.md).
    """
    if WorkspaceRole.OWNER in roles and ctx.role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can grant or modify the owner role",
        )


def require_authority_to_modify(ctx: WorkspaceContext, membership: WorkspaceMembership) -> None:
    """May ``ctx`` change or remove this existing membership?

    Admins may grant admin but may not *take it away*: otherwise any admin
    could demote every peer and leave themselves the only operator, which is a
    takeover with extra steps. Removing or demoting an admin or an owner is an
    owner's call. Acting on your own membership is always allowed — leaving a
    workspace or stepping down is not an escalation — subject to the
    last-owner rule, which no one can bypass.
    """
    target_role = WorkspaceRole(membership.role)
    if membership.user_id == ctx.user.id:
        return
    if target_role in {WorkspaceRole.ADMIN, WorkspaceRole.OWNER} and (
        ctx.role != WorkspaceRole.OWNER
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only an owner can change or remove an {target_role.value}",
        )


async def add_member(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    email: str,
    role: WorkspaceRole,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[WorkspaceMembership, User]:
    require_authority_over(ctx, role)
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
        actor_type=actor_type,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"user_id": str(user.id), "role": role.value, **(extra_metadata or {})},
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
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[WorkspaceMembership, User]:
    membership = await _get_membership(db, ctx.workspace_id, membership_id)
    current_role = WorkspaceRole(membership.role)
    require_authority_over(ctx, role, current_role)
    require_authority_to_modify(ctx, membership)
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
        actor_type=actor_type,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "from_role": current_role.value,
            "to_role": role.value,
            **(extra_metadata or {}),
        },
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
    require_authority_over(ctx, current_role)
    require_authority_to_modify(ctx, membership)
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
