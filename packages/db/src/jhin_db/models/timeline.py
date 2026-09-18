"""Ordered conversation journal and distinct public generation attempts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonDict, StdUuid, UtcDateTime, UuidPkMixin


class ConversationEvent(Base, CreatedAtMixin):
    __tablename__ = "conversation_event"
    __table_args__ = (
        Index(
            "ix_conversation_event_item", "conversation_id", "source_kind", "source_id", "sequence"
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[UUID] = mapped_column(StdUuid)
    operation: Mapped[str] = mapped_column(String(16), default="upsert")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)


class ModelGeneration(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "model_generation"
    __table_args__ = (Index("ix_model_generation_run_step", "run_id", "step"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), default=None
    )
    task_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("task.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent_run.id", ondelete="CASCADE"))
    agent_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent.id", ondelete="CASCADE"))
    step: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="running")
    text: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
