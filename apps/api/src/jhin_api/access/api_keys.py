"""Scoped API keys: mint, list, revoke, and the usage log.

The invariants that matter live here and in :mod:`jhin_domain.scopes`:

* the plaintext key exists for exactly one response and is never stored;
* a key's ``role_ceiling`` is its creator's role frozen at creation time, and
  effective permission is ``intersection(scopes, ceiling)`` — computed by
  :func:`jhin_domain.effective_scopes`, never re-derived elsewhere;
* revocation is a tombstone, not a delete, so the usage log keeps its subject.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.keys import generate_key
from jhin_api.access.schemas import ApiKeyStatus, ExpiryUnit
from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import ApiKey, ApiKeyUsage, User, WorkspaceMembership
from jhin_domain import WorkspaceRole, effective_scopes, scopes_above_role

MAX_KEYS_PER_WORKSPACE = 100
MAX_USAGE_PAGE = 200

_UNIT_DELTAS: dict[str, timedelta] = {
    "minutes": timedelta(minutes=1),
    "hours": timedelta(hours=1),
    "days": timedelta(days=1),
}


@dataclass(frozen=True)
class MintedKey:
    record: ApiKey
    plaintext: str


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def key_status(record: ApiKey, *, now: datetime | None = None) -> ApiKeyStatus:
    moment = now or datetime.now(UTC)
    if record.revoked_at is not None:
        return "revoked"
    if record.expires_at is not None and _as_utc(record.expires_at) <= moment:
        return "expired"
    return "active"


def resolve_expiry(amount: int | None, unit: ExpiryUnit, *, now: datetime) -> datetime | None:
    """Turn the picker's (amount, unit) into an absolute instant, or None."""
    if unit == "never":
        return None
    if amount is None or amount < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose how long the key should last, or select 'never expires'",
        )
    delta = _UNIT_DELTAS.get(unit)
    if delta is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Expiry unit must be minutes, hours, days, or never",
        )
    return now + delta * amount


async def list_keys(db: AsyncSession, workspace_id: UUID) -> list[tuple[ApiKey, User | None]]:
    rows = await db.execute(
        select(ApiKey, User)
        .outerjoin(User, User.id == ApiKey.created_by_user_id)
        .where(ApiKey.workspace_id == workspace_id)
        .order_by(ApiKey.created_at.desc())
    )
    return [(row[0], row[1]) for row in rows.all()]


async def create_key(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    name: str,
    scopes: list[str],
    expires_in: int | None,
    expires_unit: ExpiryUnit,
    request_id: UUID,
    ip_hash: str,
) -> MintedKey:
    if ctx.api_key is not None:
        # A key may read the key list, but minting is a human act: otherwise a
        # leaked key could quietly mint its own long-lived replacements.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot create other API keys; sign in to create one",
        )

    above = scopes_above_role(scopes, ctx.role)
    if above:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Your role ({ctx.role.value}) cannot grant: {', '.join(above)}. "
                "A key can never do more than the person who made it."
            ),
        )
    # Stored already capped, so the row can never claim more than it can do.
    granted = sorted(effective_scopes(scopes, ctx.role))
    if not granted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose at least one thing this key may do",
        )

    live = await db.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.workspace_id == ctx.workspace_id, ApiKey.revoked_at.is_(None))
    )
    if int(live or 0) >= MAX_KEYS_PER_WORKSPACE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This workspace already has {MAX_KEYS_PER_WORKSPACE} active API keys",
        )

    now = datetime.now(UTC)
    generated = generate_key()
    record = ApiKey(
        workspace_id=ctx.workspace_id,
        name=name.strip(),
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        created_by_user_id=ctx.user.id,
        role_ceiling=ctx.role.value,
        scopes_json=granted,
        expires_at=resolve_expiry(expires_in, expires_unit, now=now),
    )
    db.add(record)
    await db.flush()
    audit.record(
        db,
        action="api_key.created",
        target_type="api_key",
        target_id=record.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "prefix": record.prefix,
            "name": record.name,
            "scopes": granted,
            "role_ceiling": record.role_ceiling,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        },
    )
    await db.commit()
    return MintedKey(record=record, plaintext=generated.plaintext)


async def revoke_key(
    db: AsyncSession,
    ctx: WorkspaceContext,
    key_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    record = await db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.workspace_id == ctx.workspace_id)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    # You may always revoke your own key; revoking someone else's is an admin
    # act, because it can silently break their integration.
    if record.created_by_user_id != ctx.user.id and ctx.role not in {
        WorkspaceRole.ADMIN,
        WorkspaceRole.OWNER,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can revoke someone else's API key",
        )
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            action="api_key.revoked",
            target_type="api_key",
            target_id=record.id,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"prefix": record.prefix, "name": record.name},
        )
        await db.commit()


def _visible_usage(ctx: WorkspaceContext) -> Select[tuple[ApiKeyUsage]]:
    """Apply the usage-log visibility rule for the requesting role.

    * **Owner** — every call in the workspace.
    * **Admin** — their own key's calls, plus calls made by keys belonging to
      members and viewers. Deliberately *not* other admins' or the owner's:
      admins are peers, and peer surveillance is not part of the job.
    * **Member / viewer** — only their own.
    """
    query = select(ApiKeyUsage).where(ApiKeyUsage.workspace_id == ctx.workspace_id)
    if ctx.role == WorkspaceRole.OWNER:
        return query
    if ctx.role == WorkspaceRole.ADMIN:
        subordinate = (
            select(WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == ctx.workspace_id,
                WorkspaceMembership.role.in_(
                    [WorkspaceRole.VIEWER.value, WorkspaceRole.MEMBER.value]
                ),
            )
            .scalar_subquery()
        )
        return query.where(
            or_(
                ApiKeyUsage.acting_user_id == ctx.user.id,
                ApiKeyUsage.acting_user_id.in_(subordinate),
            )
        )
    return query.where(ApiKeyUsage.acting_user_id == ctx.user.id)


async def list_usage(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    api_key_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[ApiKeyUsage, ApiKey | None, User | None]], int]:
    limit = min(max(limit, 1), MAX_USAGE_PAGE)
    query = _visible_usage(ctx)
    if api_key_id is not None:
        query = query.where(ApiKeyUsage.api_key_id == api_key_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.execute(
        query.add_columns(ApiKey, User)
        .outerjoin(ApiKey, ApiKey.id == ApiKeyUsage.api_key_id)
        .outerjoin(User, User.id == ApiKeyUsage.acting_user_id)
        .order_by(ApiKeyUsage.created_at.desc(), ApiKeyUsage.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1], row[2]) for row in rows.all()], int(total)


async def record_usage(
    db: AsyncSession,
    *,
    api_key_id: UUID,
    workspace_id: UUID,
    acting_user_id: UUID,
    method: str,
    path: str,
    status_code: int,
    ip_hash: str,
    retention_days: int,
) -> None:
    """Append one usage row, occasionally pruning past the retention window.

    Pruning is sampled rather than scheduled so the write path stays a single
    INSERT for ~99% of requests and the table still cannot grow without bound
    on an instance that never runs maintenance.
    """
    db.add(
        ApiKeyUsage(
            workspace_id=workspace_id,
            api_key_id=api_key_id,
            acting_user_id=acting_user_id,
            method=method[:8],
            path=path[:300],
            status_code=status_code,
            ip_hash=ip_hash,
        )
    )
    if retention_days > 0 and random.random() < 0.01:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        await db.execute(delete(ApiKeyUsage).where(ApiKeyUsage.created_at < cutoff))
    await db.commit()
