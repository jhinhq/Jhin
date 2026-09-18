"""Schedule validation and storage shared by the API and authorized tools.

PostgreSQL owns occurrences; Temporal wakes and resumes their exact task ids.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from jhin_db.models import Agent, AuditEvent, Workspace
from jhin_db.models.schedule import AgentSchedule
from jhin_domain import new_uuid7
from jhin_memory.screening import screen_content


class ScheduleError(ValueError):
    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.status_code = status_code


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WeekdayName = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WeekdayIndex = Annotated[int, Field(ge=0, le=6, strict=True)]
WEEKDAY_DESCRIPTION = (
    "Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6."
)


def timezone_key(value: str) -> str:
    value = value.strip()
    if value != "UTC" and "/" not in value:
        raise ValueError("Use an IANA timezone such as America/Los_Angeles, or UTC")
    try:
        return ZoneInfo(value).key
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Unknown IANA timezone") from exc


def clock_time(value: str) -> str:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Use local time HH:MM") from exc
    if len(value) != 5 or parsed.tzinfo is not None or parsed.second:
        raise ValueError("Use local time HH:MM")
    return value


def next_occurrence(
    after: datetime,
    local_time: str,
    timezone: str,
    weekdays: list[int] | tuple[int, ...] = tuple(range(7)),
) -> datetime:
    """Strictly after, skip nonexistent wall time, first fold only."""
    zone = ZoneInfo(timezone_key(timezone))
    target = time.fromisoformat(clock_time(local_time))
    if not weekdays or any(day not in range(7) for day in weekdays):
        raise ValueError("Select at least one weekday (Monday=0)")
    after = (after if after.tzinfo is not None else after.replace(tzinfo=UTC)).astimezone(UTC)
    date = after.astimezone(zone).date()
    for offset in range(370):
        day = date + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        wall = datetime.combine(day, target)
        instant = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) != wall:
            continue
        if instant > after:
            return instant
    raise ValueError("No upcoming occurrence")


class ScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=12000)
    local_time: str
    timezone: str = Field(max_length=64)
    weekdays: list[WeekdayIndex] = Field(
        default_factory=lambda: list(range(7)),
        min_length=1,
        max_length=7,
        description=WEEKDAY_DESCRIPTION,
    )
    enabled: bool = True
    overlap_policy: Literal["skip"] = "skip"

    _timezone = field_validator("timezone")(timezone_key)
    _time = field_validator("local_time")(clock_time)

    @field_validator("weekdays")
    @classmethod
    def days(cls, value: list[int]) -> list[int]:
        if any(day not in range(7) for day in value):
            raise ValueError("Weekdays must be 0 through 6")
        return sorted(set(value))

    @field_validator("brief", "name")
    @classmethod
    def no_secrets(cls, value: str) -> str:
        screened = screen_content(value)
        if screened.rejected or screened.redacted:
            raise ValueError("Keep credentials in sensitive variables, not the schedule brief")
        return value


class ScheduleCreate(ScheduleFields):
    agent_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    name: str | None = None
    brief: str | None = None
    local_time: str | None = None
    timezone: str | None = None
    weekdays: list[WeekdayIndex] | None = Field(default=None, description=WEEKDAY_DESCRIPTION)
    enabled: bool | None = None


class ScheduleOut(ScheduleFields):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    version: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def weekday_names(self) -> list[str]:
        return [WEEKDAY_NAMES[day] for day in self.weekdays]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_run_local(self) -> str | None:
        if self.next_run_at is None:
            return None
        instant = self.next_run_at
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        return instant.astimezone(ZoneInfo(self.timezone)).isoformat()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_run_display(self) -> str | None:
        if self.next_run_at is None:
            return None
        instant = self.next_run_at
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        local = instant.astimezone(ZoneInfo(self.timezone))
        return (
            f"{WEEKDAY_NAMES[local.weekday()]}, {local.strftime('%B')} {local.day}, "
            f"{local.year} at {local:%H:%M %Z} ({self.timezone})"
        )


async def get_schedule(
    db: AsyncSession, workspace_id: UUID, schedule_id: UUID, *, lock: bool = False
) -> AgentSchedule:
    query = select(AgentSchedule).where(
        AgentSchedule.id == schedule_id, AgentSchedule.workspace_id == workspace_id
    )
    row = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if row is None:
        raise ScheduleError("Schedule not found", 404)
    if row.next_run_at is not None and row.next_run_at.tzinfo is None:
        # SQLite returns stored UTC timestamps without offsets. Refreshing a
        # cached row must keep the same aware-UTC contract as PostgreSQL.
        set_committed_value(row, "next_run_at", row.next_run_at.replace(tzinfo=UTC))
    return row


def _audit(
    db: AsyncSession, row: AgentSchedule, action: str, user_id: UUID | None, agent_id: UUID | None
) -> None:
    db.add(
        AuditEvent(
            workspace_id=row.workspace_id,
            actor_type="user" if user_id else "agent",
            actor_id=user_id or agent_id,
            action=action,
            target_type="agent_schedule",
            target_id=row.id,
            metadata_json={
                "version": row.version,
                "timezone": row.timezone,
                "enabled": row.enabled,
            },
        )
    )


async def create_schedule(
    db: AsyncSession,
    workspace_id: UUID,
    data: ScheduleCreate,
    *,
    user_id: UUID | None = None,
    agent_id: UUID | None = None,
    now: datetime | None = None,
) -> AgentSchedule:
    digest = hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    # Serialize the workspace-wide idempotency key before inspecting its owner.
    await db.scalar(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update(key_share=True)
    )
    owner = await db.scalar(
        select(Agent).where(Agent.id == data.agent_id, Agent.workspace_id == workspace_id)
    )
    if owner is None or owner.status != "active":
        raise ScheduleError("Choose an active agent in this workspace", 404)
    existing = await db.scalar(
        select(AgentSchedule).where(
            AgentSchedule.workspace_id == workspace_id,
            AgentSchedule.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise ScheduleError("This idempotency key already names a different schedule", 409)
        return existing
    row = AgentSchedule(
        id=new_uuid7(),
        workspace_id=workspace_id,
        **data.model_dump(),
        request_hash=digest,
        version=1,
        created_by_user_id=user_id,
        created_by_agent_id=agent_id,
    )
    row.next_run_at = (
        next_occurrence(now or datetime.now(UTC), row.local_time, row.timezone, row.weekdays)
        if row.enabled
        else None
    )
    db.add(row)
    _audit(db, row, "schedule.created", user_id, agent_id)
    await db.flush()
    return row


async def update_schedule(
    db: AsyncSession,
    workspace_id: UUID,
    schedule_id: UUID,
    data: ScheduleUpdate,
    *,
    user_id: UUID | None = None,
    agent_id: UUID | None = None,
    now: datetime | None = None,
) -> AgentSchedule:
    row = await get_schedule(db, workspace_id, schedule_id, lock=True)
    if row.deleted_at is not None or row.version != data.expected_version:
        raise ScheduleError("Schedule changed; reload before editing", 409)
    changes = data.model_dump(exclude={"expected_version"}, exclude_unset=True)
    fields = ScheduleFields.model_validate(
        {**{key: getattr(row, key) for key in ScheduleFields.model_fields}, **changes}
    )
    for key, value in fields.model_dump().items():
        setattr(row, key, value)
    row.version += 1
    if changes.keys() & {"local_time", "timezone", "weekdays", "enabled"}:
        row.next_run_at = (
            next_occurrence(now or datetime.now(UTC), row.local_time, row.timezone, row.weekdays)
            if row.enabled
            else None
        )
    _audit(db, row, "schedule.updated", user_id, agent_id)
    from jhin_tools.schedule_confirmation import retire_confirmation_questions

    await retire_confirmation_questions(db, row)
    await db.flush()
    return row


async def delete_schedule(
    db: AsyncSession,
    workspace_id: UUID,
    schedule_id: UUID,
    expected_version: int,
    *,
    user_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> None:
    row = await get_schedule(db, workspace_id, schedule_id, lock=True)
    if row.deleted_at is not None:
        return
    if row.version != expected_version:
        raise ScheduleError("Schedule changed; reload before deleting", 409)
    row.enabled = False
    row.next_run_at = None
    row.deleted_at = datetime.now(UTC)
    row.version += 1
    _audit(db, row, "schedule.deleted", user_id, agent_id)
    from jhin_tools.schedule_confirmation import retire_confirmation_questions

    await retire_confirmation_questions(db, row)
    await db.flush()
