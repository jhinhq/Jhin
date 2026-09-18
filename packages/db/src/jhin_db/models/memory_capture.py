"""Prospective human-admin authority for selected future memory extracts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class MemoryCapturePolicy(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "memory_capture_policy"
    __table_args__ = (Index("ix_memory_capture_destination", "workspace_id", "scope", "scope_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID] = mapped_column(StdUuid)
    granted_by_user_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="CASCADE")
    )
    allowed_classes_json: Mapped[list[str]] = mapped_column(JsonList)
    actor_ids_json: Mapped[list[str]] = mapped_column(JsonList)
    allowed_source_agent_ids_json: Mapped[list[str]] = mapped_column(JsonList, default=list)
    # Human statements by this user only. A conversation restriction may narrow
    # it further; NULL permits their future statements in other eligible chats.
    source_user_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("user.id", ondelete="CASCADE"))
    source_conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), default=None
    )
    effective_from: Mapped[datetime] = mapped_column(UtcDateTime)
    source_after_message_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
