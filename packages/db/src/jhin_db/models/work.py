"""Tasks, agent runs, messages, and run events (plan 6.12-6.14).

Postgres is the source of truth for all of these; NATS only transports the
same facts to live consumers (plan 2.3, 9.1). ``run_event`` persists the
structured execution timeline the UI renders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonDict, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import MessageVisibility, RunStatus, TaskPriority, TaskState


class Task(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "task"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    external_source: Mapped[str | None] = mapped_column(String(64), default=None)
    external_id: Mapped[str | None] = mapped_column(String(200), default=None)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default=TaskState.QUEUED.value, index=True)
    priority: Mapped[str] = mapped_column(String(16), default=TaskPriority.NORMAL.value)
    assigned_agent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None, index=True
    )
    assigned_team_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("team.id", ondelete="SET NULL"), default=None
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    trigger_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(200), default=None)
    correlation_id: Mapped[UUID] = mapped_column(Uuid)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)


class AgentRun(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "agent_run"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task.id", ondelete="SET NULL"), default=None, index=True
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    model_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("model_profile.id", ondelete="SET NULL"), default=None
    )
    # Immutable configuration hash (plan 7.1): which exact snapshot ran.
    snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    steps_used: Mapped[int] = mapped_column(Integer, default=0)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(200), default=None)
    temporal_run_id: Mapped[str | None] = mapped_column(String(200), default=None)
    langgraph_thread_id: Mapped[str | None] = mapped_column(String(200), default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)


class Message(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "message"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task.id", ondelete="CASCADE"), default=None, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None, index=True
    )
    sender_type: Mapped[str] = mapped_column(String(16))
    sender_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    recipient_type: Mapped[str] = mapped_column(String(16))
    recipient_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    content_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    visibility: Mapped[str] = mapped_column(String(16), default=MessageVisibility.VISIBLE.value)


class RunEvent(Base, UuidPkMixin, CreatedAtMixin):
    """Persisted structured execution event — the task timeline's substance.

    Append-only by convention: rows are only ever inserted by activities.
    """

    __tablename__ = "run_event"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("task.id", ondelete="CASCADE"), default=None, index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
