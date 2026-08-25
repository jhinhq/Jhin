"""Append-only audit recording and admin queries (plan sections 6.17 and 23).

``record`` only ever INSERTs. There is intentionally no update or delete
function in this module, and none may be added.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import AuditEvent
from jhin_domain import ActorType

MAX_PAGE_SIZE = 200


def record(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    workspace_id: UUID | None,
    actor_type: ActorType = ActorType.USER,
    actor_id: UUID | None = None,
    target_id: UUID | None = None,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Stage an audit row in the caller's transaction (committed with it)."""
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_type=actor_type.value,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata_json=metadata or {},
        request_id=request_id,
        ip_hash=ip_hash,
    )
    session.add(event)
    return event


async def list_events(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    actor_id: UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    """Newest-first audit page for one workspace, plus the total match count."""
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    offset = max(offset, 0)
    query = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
    if actor_id is not None:
        query = query.where(AuditEvent.actor_id == actor_id)
    if action:
        query = query.where(AuditEvent.action == action)
    if target_type:
        query = query.where(AuditEvent.target_type == target_type)
    if created_from is not None:
        query = query.where(AuditEvent.created_at >= created_from)
    if created_to is not None:
        query = query.where(AuditEvent.created_at <= created_to)

    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(
        query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total)
