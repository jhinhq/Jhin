"""A bounded current summary, never a stale second store of remembered claims."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_serializer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import DbSession, MemberCtx, ViewerCtx
from jhin_api.memory.service import _require_scope_authority
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import Agent, MemoryRecord, Team
from jhin_domain import MemoryScope
from jhin_memory.evidence import supported_record
from jhin_secrets.intake import redact_legacy_text

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/memories",
    tags=["memory"],
    dependencies=[Depends(csrf_protect)],
)


class SummaryItem(BaseModel):
    id: UUID
    version: int
    content: str
    source_conversation_id: UUID | None
    source_message_id: UUID | None
    source_task_id: UUID | None

    @field_serializer("content")
    def serialize_legacy_content(self, value: str) -> str:
        return redact_legacy_text(value)


class MemorySummary(BaseModel):
    scope: str
    scope_id: UUID
    version: str
    summary: str
    items: list[SummaryItem]
    coverage_count: int
    source_count: int
    generated_at: datetime | None
    stale: bool = False

    @field_serializer("summary")
    def serialize_legacy_summary(self, value: str) -> str:
        return redact_legacy_text(value)


async def build_summary(
    db: AsyncSession, workspace_id: UUID, scope: MemoryScope, scope_id: UUID
) -> MemorySummary:
    if scope is MemoryScope.WORKSPACE:
        valid = scope_id == workspace_id
    else:
        model = Agent if scope is MemoryScope.AGENT else Team
        valid = (
            await db.scalar(
                select(model.id).where(model.id == scope_id, model.workspace_id == workspace_id)
            )
            is not None
        )
    if not valid:
        raise HTTPException(404, "Memory scope not found")
    rows = list(
        await db.scalars(
            select(MemoryRecord)
            .where(
                MemoryRecord.workspace_id == workspace_id,
                MemoryRecord.scope == scope.value,
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.status == "active",
                or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > datetime.now(UTC)),
            )
            .order_by(
                MemoryRecord.importance.desc(), MemoryRecord.updated_at.desc(), MemoryRecord.id
            )
            .limit(500)
        )
    )
    supported = [row for row in rows if supported_record(row)]
    selected: list[MemoryRecord] = []
    subjects = set()
    for row in supported:
        key = row.subject or row.content_hash
        if key in subjects:
            continue
        subjects.add(key)
        if len(selected) < 20:
            selected.append(row)
    version = hashlib.sha256(
        json.dumps(
            [
                (str(row.id), row.version, row.updated_at.isoformat(), row.content_hash)
                for row in rows
            ]
        ).encode()
    ).hexdigest()[:20]
    items = [
        SummaryItem(
            id=row.id,
            version=row.version,
            content=row.content,
            source_conversation_id=row.source_conversation_id,
            source_message_id=row.source_message_id,
            source_task_id=row.source_task_id,
        )
        for row in selected
    ]
    return MemorySummary(
        scope=scope.value,
        scope_id=scope_id,
        version=version,
        summary="\n".join(f"- {row.content}" for row in selected),
        items=items,
        coverage_count=len(items),
        source_count=len(supported),
        generated_at=max((row.updated_at for row in rows), default=None),
    )


@router.get("/summary")
async def summary(
    ctx: ViewerCtx, db: DbSession, scope: MemoryScope, scope_id: UUID
) -> MemorySummary:
    return await build_summary(db, ctx.workspace_id, scope, scope_id)


@router.post("/summary/rebuild")
async def rebuild(
    ctx: MemberCtx, db: DbSession, scope: MemoryScope, scope_id: UUID
) -> MemorySummary:
    _require_scope_authority(ctx, scope)
    return await build_summary(db, ctx.workspace_id, scope, scope_id)
