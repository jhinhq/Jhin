"""Versioned local Ghost archive inventory and complete available article bodies."""

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, StdUuid, TimestampMixin, UuidPkMixin


class BlogCorpusSync(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "blog_corpus_sync"
    __table_args__ = (
        UniqueConstraint("workspace_id", "active_key", name="uq_corpus_active_sync"),
        UniqueConstraint("workspace_id", "request_key", name="uq_corpus_request"),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE")
    )
    assignment_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("editorial_assignment.id", ondelete="CASCADE")
    )
    agent_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent.id", ondelete="CASCADE"))
    task_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("task.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent_run.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    active_key: Mapped[str | None] = mapped_column(String(100), default=None)
    request_key: Mapped[str | None] = mapped_column(String(100), default=None)
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    pass_number: Mapped[int] = mapped_column(Integer, default=1)
    expected_total: Mapped[int | None] = mapped_column(Integer, default=None)
    discovered: Mapped[int] = mapped_column(Integer, default=0)
    indexed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    previous_manifest: Mapped[str | None] = mapped_column(String(64), default=None)
    corpus_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
    index_version: Mapped[str] = mapped_column(String(50), default="lexical-v1")


class BlogCorpusDocument(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "blog_corpus_document"
    __table_args__ = (UniqueConstraint("sync_id", "post_id", name="uq_corpus_document_version"),)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    sync_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("blog_corpus_sync.id", ondelete="CASCADE"), index=True
    )
    post_id: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    provider_revision: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    complete: Mapped[bool] = mapped_column(default=True)
    seen_pass: Mapped[int] = mapped_column(Integer)
