"""Conversation files and immutable revisions, independent of sandbox lifetime."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonDict, JsonList, StdUuid, TimestampMixin, UuidPkMixin


class ChatProject(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "chat_project"
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    repository_url: Mapped[str | None] = mapped_column(String(2000), default=None)
    source_revision: Mapped[str | None] = mapped_column(String(300), default=None)
    # A saved project owns its blob references independently of the source chat.
    source_manifest_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    context: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )


class ManagedFile(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "managed_file"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "conversation_id", "path", name="uq_managed_file_conversation_path"
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(16), default="upload")
    status: Mapped[str] = mapped_column(String(16), default="ready")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Validated by publication transaction. Avoids a cascade cycle with revisions.
    current_revision_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )


class FileRevision(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "file_revision"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_file_revision_version"),)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("managed_file.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(200))
    preview_kind: Mapped[str] = mapped_column(String(20))
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    extraction_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )


class FileCheckpoint(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "file_checkpoint"
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(200), default="Checkpoint")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    excluded_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )


class FileAnnotation(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "file_annotation"
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("managed_file.id", ondelete="CASCADE"), index=True
    )
    revision_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("file_revision.id", ondelete="CASCADE")
    )
    text: Mapped[str] = mapped_column(Text)
    location_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
