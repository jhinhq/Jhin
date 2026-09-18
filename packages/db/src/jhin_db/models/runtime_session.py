"""Durable authority for browser sessions and contained workspace operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class RuntimeSession(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "runtime_session"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="starting", index=True)
    workspace_key: Mapped[str] = mapped_column(String(96))
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    network: Mapped[str] = mapped_column(String(16), default="none")
    ticket_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    ticket_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None, index=True)
    # Exact operation/request authority. File bytes are never recorded here.
    config_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    state_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
