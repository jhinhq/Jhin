"""Curated long-term memory: versioned, scoped ``memory_record`` rows.

Each row is one immutable *version* of a memory. Edits create a new row whose
``supersedes_id`` points at the previous version (which becomes
``superseded``). Forgetting keeps the row as a content-free tombstone so the
audit trail retains identifiers without any live content or embedding.

The embedding is stored portably as a JSON list (``embedding_json``); on
PostgreSQL the ``0016`` migration additionally creates a raw ``embedding_vec
vector`` column when pgvector is available. That column is never ORM-mapped
(the schema must stay valid without the extension) and is maintained by
:mod:`jhin_memory` through dialect-aware SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import MemorySensitivity, MemoryStatus


class MemoryRecord(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "memory_record"
    __table_args__ = (
        Index("ix_memory_record_workspace_scope", "workspace_id", "scope", "scope_id", "status"),
        Index("ix_memory_record_workspace_hash", "workspace_id", "content_hash"),
        Index("ix_memory_record_workspace_subject", "workspace_id", "subject"),
        Index("ix_memory_record_expires_at", "expires_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    # agent | team | workspace; scope_id is the agent/team/workspace id.
    scope: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID] = mapped_column(StdUuid)
    kind: Mapped[str] = mapped_column(String(32))
    # Optional normalized subject key used for contradiction detection
    # ("deploy.day", "user.timezone"); free-form memories leave it null.
    subject: Mapped[str | None] = mapped_column(String(200), default=None)
    # Empty once forgotten (tombstone).
    content: Mapped[str] = mapped_column(Text, default="")
    # sha256 of the normalized content; "" once forgotten.
    content_hash: Mapped[str] = mapped_column(String(64), default="")

    source_conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="SET NULL"), default=None
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("message.id", ondelete="SET NULL"), default=None
    )
    source_task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    source_event_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)

    # The source's visibility ceiling (agent|team|workspace). Non-amplification:
    # ``scope`` may never be broader than ``visibility``.
    visibility: Mapped[str] = mapped_column(String(16))
    sensitivity: Mapped[str] = mapped_column(
        String(16), default=MemorySensitivity.NORMAL.value, server_default=text("'normal'")
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    importance: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    tags_json: Mapped[list[str]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(
        String(16), default=MemoryStatus.PROPOSED.value, server_default=text("'proposed'")
    )
    valid_from: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    pinned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    forgotten_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("memory_record.id", ondelete="SET NULL"), default=None
    )

    # Portable embedding: JSON list of floats (null when absent or forgotten).
    embedding_json: Mapped[list[float] | None] = mapped_column(JsonList, default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(200), default=None)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, default=None)

    # user | agent | system
    created_by_type: Mapped[str] = mapped_column(String(16))
    created_by_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    # Policy evidence (decision reasons, redaction flags) — never content.
    policy_json: Mapped[dict[str, Any]] = mapped_column(
        JsonList, default=dict, server_default=text("'{}'")
    )
