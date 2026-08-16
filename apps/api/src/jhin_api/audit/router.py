"""Route handler for /api/v1/workspaces/{workspace_id}/audit-events.

Read-only and admin-only. There are no write routes: audit rows are
append-only and only ever created by services (plan 23).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from jhin_api.audit import service
from jhin_api.audit.schemas import AuditEventOut, AuditEventPage
from jhin_api.deps import AdminCtx, DbSession

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["audit"])


@router.get("/audit-events")
async def list_audit_events(
    ctx: AdminCtx,
    db: DbSession,
    actor_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=120)] = None,
    target_type: Annotated[str | None, Query(max_length=60)] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditEventPage:
    events, total = await service.list_events(
        db,
        ctx.workspace_id,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return AuditEventPage(
        events=[AuditEventOut.model_validate(event, from_attributes=True) for event in events],
        total=total,
    )
