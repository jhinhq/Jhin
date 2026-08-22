"""Conversations: named, persistent threads between a human and one agent.

Every user turn that needs agent work becomes a ``task`` linked to the
conversation, so the task engine stays the execution authority and the
conversation is the chat-first view over those work episodes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import ConversationStatus


class Conversation(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "conversation"
    __table_args__ = (
        Index(
            "ix_conversation_workspace_last_activity",
            "workspace_id",
            text("last_activity_at DESC"),
        ),
        Index("ix_conversation_workspace_primary_agent", "workspace_id", "primary_agent_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(16), default=ConversationStatus.ACTIVE.value, server_default=text("'active'")
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    primary_agent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    last_activity_at: Mapped[datetime] = mapped_column(UtcDateTime)
