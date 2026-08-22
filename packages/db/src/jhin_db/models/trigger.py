"""Triggers and their invocations (plan 6.11, 9.4, 10).

A trigger is connector-agnostic configuration: WHEN (connection + canonical
event type) / IF (safe JSON filter, see ``jhin_triggers``) / THEN (start a
task for an agent). ``trigger_invocation`` is the durable idempotency ledger:
one row per match, with a deterministic idempotency key that is unique among
*started* invocations — a semantically identical repeat within the dedupe
window records a ``duplicate`` row and starts nothing (plan 48.6).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonDict, StdUuid, TimestampMixin, UuidPkMixin
from jhin_domain import TriggerActionType, TriggerType

# Two events for the same transition inside this window collapse into one
# invocation (plan 9.4). Override per trigger.
DEFAULT_DEDUPE_WINDOW_SECONDS = 300


class Trigger(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "trigger"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default=TriggerType.CONNECTOR_EVENT.value)
    connection_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE"), default=None, index=True
    )
    # Canonical event type this trigger listens for, e.g.
    # ``connector.linear.issue.updated``.
    event_type: Mapped[str | None] = mapped_column(String(200), default=None, index=True)
    # Safe JSON filter DSL (plan 10.2) — evaluated by jhin_triggers, never code.
    filter_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    action_type: Mapped[str] = mapped_column(
        String(32), default=TriggerActionType.START_AGENT_TASK.value
    )
    target_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    target_team_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("team.id", ondelete="SET NULL"), default=None
    )
    # Reserved for workflow templates (plan 6.11); unused by start_agent_task.
    workflow_definition: Mapped[dict[str, Any] | None] = mapped_column(JsonDict, default=None)
    # Action tuning, e.g. {"comment_back": true} to post a status comment on
    # the source entity when the task finishes (plan 26.14).
    action_config_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    dedupe_window_seconds: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_DEDUPE_WINDOW_SECONDS
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )


class TriggerInvocation(Base, UuidPkMixin, CreatedAtMixin):
    """One trigger match and what became of it (plan 9.4, 17.10).

    The partial unique index on (trigger_id, idempotency_key) where
    status='started' is the database-level dedupe authority: only one
    *started* invocation may exist per key, while duplicate/failed attempts
    remain recordable for the UI.
    """

    __tablename__ = "trigger_invocation"
    __table_args__ = (
        Index(
            "uq_trigger_invocation_started_key",
            "trigger_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status = 'started'"),
            sqlite_where=text("status = 'started'"),
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    trigger_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("trigger.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    # The canonical event that matched (envelope event_id).
    event_id: Mapped[UUID] = mapped_column(StdUuid)
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None, index=True
    )
    workflow_id: Mapped[str | None] = mapped_column(String(200), default=None)
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)
