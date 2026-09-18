"""Workspace schedule configuration and retained execution history."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.security.csrf import csrf_protect
from jhin_db.models.schedule import AgentSchedule, ScheduleOccurrence
from jhin_tools.scheduling import (
    ScheduleCreate,
    ScheduleError,
    ScheduleOut,
    ScheduleUpdate,
    create_schedule,
    delete_schedule,
    get_schedule,
    update_schedule,
)


async def schedule_errors() -> AsyncIterator[None]:
    try:
        yield
    except ScheduleError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            422, "Invalid schedule; verify time, timezone, brief and weekdays"
        ) from exc


router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/schedules",
    tags=["automations"],
    dependencies=[Depends(csrf_protect), Depends(schedule_errors)],
)


class ScheduleList(BaseModel):
    items: list[ScheduleOut]
    total: int


class OccurrenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    schedule_id: UUID
    scheduled_for: datetime
    status: str
    task_id: UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str


class OccurrenceList(BaseModel):
    items: list[OccurrenceOut]
    total: int


@router.get("")
async def list_schedules(
    ctx: ViewerCtx,
    db: DbSession,
    agent_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ScheduleList:
    query = select(AgentSchedule).where(
        AgentSchedule.workspace_id == ctx.workspace_id, AgentSchedule.deleted_at.is_(None)
    )
    if agent_id:
        query = query.where(AgentSchedule.agent_id == agent_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(AgentSchedule.created_at.desc(), AgentSchedule.id)
        .limit(limit)
        .offset(offset)
    )
    return ScheduleList(items=[ScheduleOut.model_validate(row) for row in rows], total=total or 0)


@router.post("", status_code=201)
async def create(payload: ScheduleCreate, ctx: AdminCtx, db: DbSession) -> ScheduleOut:
    row = await create_schedule(db, ctx.workspace_id, payload, user_id=ctx.user.id)
    await db.commit()
    return ScheduleOut.model_validate(row)


@router.get("/{schedule_id}")
async def get(schedule_id: UUID, ctx: ViewerCtx, db: DbSession) -> ScheduleOut:
    return ScheduleOut.model_validate(await get_schedule(db, ctx.workspace_id, schedule_id))


@router.patch("/{schedule_id}")
async def update(
    schedule_id: UUID, payload: ScheduleUpdate, ctx: AdminCtx, db: DbSession
) -> ScheduleOut:
    row = await update_schedule(db, ctx.workspace_id, schedule_id, payload, user_id=ctx.user.id)
    await db.commit()
    return ScheduleOut.model_validate(row)


@router.delete("/{schedule_id}", status_code=204)
async def delete(
    schedule_id: UUID, ctx: AdminCtx, db: DbSession, expected_version: Annotated[int, Query(ge=1)]
) -> Response:
    await delete_schedule(db, ctx.workspace_id, schedule_id, expected_version, user_id=ctx.user.id)
    await db.commit()
    return Response(status_code=204)


@router.get("/{schedule_id}/occurrences")
async def occurrences(
    schedule_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OccurrenceList:
    await get_schedule(db, ctx.workspace_id, schedule_id)
    query = select(ScheduleOccurrence).where(
        ScheduleOccurrence.workspace_id == ctx.workspace_id,
        ScheduleOccurrence.schedule_id == schedule_id,
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(
        query.order_by(ScheduleOccurrence.scheduled_for.desc(), ScheduleOccurrence.id)
        .limit(limit)
        .offset(offset)
    )
    return OccurrenceList(
        items=[OccurrenceOut.model_validate(row) for row in rows], total=total or 0
    )
