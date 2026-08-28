"""Conversations: named, persistent threads between a human and one agent,
and the questions an agent asks the person inside one.

Every user turn that needs agent work becomes a ``task`` linked to the
conversation, so the task engine stays the execution authority and the
conversation is the chat-first view over those work episodes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import ConversationStatus, UserQuestionStatus


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
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(16), default=ConversationStatus.ACTIVE.value, server_default=text("'active'")
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    primary_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    last_activity_at: Mapped[datetime] = mapped_column(UtcDateTime)


class UserQuestion(Base, UuidPkMixin, TimestampMixin):
    """One question an agent put to the person it is talking to.

    The row — not the model's account of it — is the authority for what was
    asked, what was answered, and whether that answer authorised a wider
    memory. ``granted_*`` is written only by the API at answer time, from the
    answering user's RBAC; a worker and a model may read it and nothing more.
    """

    __tablename__ = "user_question"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_user_question_workspace_idempotency"
        ),
        CheckConstraint("status IN ('pending', 'answered', 'expired', 'cancelled')", name="status"),
        CheckConstraint(
            "granted_scope IN ('', 'agent', 'team', 'workspace')", name="granted_scope"
        ),
        CheckConstraint("answer_kind IN ('', 'option', 'other')", name="answer_kind"),
        Index("ix_user_question_workspace_status", "workspace_id", "status"),
        Index(
            "ix_user_question_workspace_conversation",
            "workspace_id",
            "conversation_id",
            "status",
        ),
        Index(
            "ix_user_question_workspace_agent_dedupe",
            "workspace_id",
            "agent_id",
            "dedupe_hash",
        ),
        Index("ix_user_question_workspace_run", "workspace_id", "run_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="SET NULL"), default=None
    )
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent.id", ondelete="CASCADE"))
    # The chat row the person answers from; SET NULL so forgetting a
    # conversation's messages never deletes the audit of what was asked.
    message_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("message.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(32), default="open", server_default=text("'open'"))
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    options_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    allow_other: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # sha256 over the normalized question plus its option values: the repeat
    # guard, so an agent cannot ask the same thing twice in one conversation.
    dedupe_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(16),
        default=UserQuestionStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    asked_at: Mapped[datetime] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    answered_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    # Derived from *which field* the answer arrived in, never by comparing
    # text: a person who types the exact words of an option still typed them.
    answer_kind: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    answer_option_value: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''")
    )
    answer_text: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    granted_scope: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    # The answering user's RBAC ceiling at answer time, so a role change later
    # cannot retroactively widen or narrow what they authorised.
    granted_authority: Mapped[str] = mapped_column(
        String(16), default="", server_default=text("''")
    )
    grant_denied_reason: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''")
    )
    # One answer is worth one memory: stamped when memory.propose spends it.
    grant_consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    grant_consumed_tool_call_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
