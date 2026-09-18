"""Durable local-time schedules and an occurrence idempotency ledger."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class AgentSchedule(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "agent_schedule"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str] = mapped_column(Text)
    local_time: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(64))
    weekdays: Mapped[list[int]] = mapped_column(JsonList, default=lambda: list(range(7)))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    overlap_policy: Mapped[str] = mapped_column(String(16), default="skip")
    version: Mapped[int] = mapped_column(Integer, default=1)
    next_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_status: Mapped[str] = mapped_column(String(32), default="")
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_by_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )


class ScheduleOccurrence(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "schedule_occurrence"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for"),
        Index("ix_schedule_occurrence_history", "workspace_id", "schedule_id", "scheduled_for"),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    schedule_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent_schedule.id", ondelete="CASCADE")
    )
    scheduled_for: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(32), default="running")
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    error_code: Mapped[str] = mapped_column(String(100), default="")
