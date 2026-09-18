"""Immutable provider draft snapshots and separately recorded publication decisions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class GhostInstallation(Base, UuidPkMixin, TimestampMixin):
    """One publishing identity per confirmed canonical installation, across keys."""

    __tablename__ = "ghost_installation"
    __table_args__ = (UniqueConstraint("workspace_id", "admin_url"),)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    admin_url: Mapped[str] = mapped_column(String(2000))
    publisher_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="RESTRICT")
    )


class EditorialAssignment(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "editorial_assignment"
    __table_args__ = (
        CheckConstraint(
            "release_intent IN ('draft_only','publish_after_ashley_review')",
            name="editorial_release_intent",
        ),
        UniqueConstraint("connection_id", "post_id", name="uq_editorial_owned_post"),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="RESTRICT")
    )
    writer_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="RESTRICT")
    )
    publisher_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="SET NULL"), default=None
    )
    team_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("team.id", ondelete="SET NULL"), default=None
    )
    public_blog_url: Mapped[str] = mapped_column(String(2000), default="")
    post_id: Mapped[str | None] = mapped_column(String(24), default=None)
    release_intent: Mapped[str] = mapped_column(
        String(40), default="draft_only", server_default="draft_only"
    )
    brief_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    decision_provenance: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    evidence_tool_call_ids: Mapped[list[str]] = mapped_column(JsonList, default=list)
    brief_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    editorial_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    phase: Mapped[str] = mapped_column(String(40), default="brief", server_default="brief")
    blocked_reason: Mapped[str | None] = mapped_column(Text, default=None)
    resume_condition: Mapped[str | None] = mapped_column(Text, default=None)


class EditorialReviewPackage(Base, UuidPkMixin, TimestampMixin):
    """Immutable envelope; GhostEditorialReview remains the only decision record."""

    __tablename__ = "editorial_review_package"
    __table_args__ = (UniqueConstraint("assignment_id", "revision"),)
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    assignment_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("editorial_assignment.id", ondelete="CASCADE")
    )
    editorial_version: Mapped[int] = mapped_column(Integer)
    revision: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JsonDict)


class GhostReviewReadReceipt(Base, UuidPkMixin, TimestampMixin):
    """Server-recorded complete chunk delivery for an exact provider revision."""

    __tablename__ = "ghost_review_read_receipt"
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE")
    )
    agent_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("agent.id", ondelete="CASCADE"))
    post_id: Mapped[str] = mapped_column(String(24))
    revision: Mapped[str] = mapped_column(String(64))
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    total_chars: Mapped[int] = mapped_column(Integer)
    package_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("editorial_review_package.id", ondelete="CASCADE"), default=None
    )


class GhostEditorialReview(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "ghost_editorial_review"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "post_id",
            "revision",
            "publisher_agent_id",
            "package_id",
            name="uq_ghost_review_revision_publisher",
        ),
        CheckConstraint(
            "status IN ('pending','approved','changes_requested',"
            "'stale','publishing','published','uncertain')",
            name="ghost_review_status",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE"), index=True
    )
    post_id: Mapped[str] = mapped_column(String(24))
    revision: Mapped[str] = mapped_column(String(64))
    admin_url: Mapped[str] = mapped_column(String(2000))
    provider_updated_at: Mapped[str] = mapped_column(String(50))
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JsonDict)
    author_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="RESTRICT")
    )
    publisher_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="RESTRICT"), index=True
    )
    work_review_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("work_review.id", ondelete="SET NULL"), default=None
    )
    work_request_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("work_request.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    publication_tool_call_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    assignment_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("editorial_assignment.id", ondelete="RESTRICT"), default=None
    )
    package_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("editorial_review_package.id", ondelete="RESTRICT"), default=None
    )
    assignment_editorial_version: Mapped[int | None] = mapped_column(Integer, default=None)
    release_intent: Mapped[str] = mapped_column(
        String(40), default="draft_only", server_default="draft_only"
    )
    revision_round: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    prior_review_id: Mapped[UUID | None] = mapped_column(
        StdUuid,
        ForeignKey(
            "ghost_editorial_review.id", ondelete="SET NULL", name="fk_ghost_review_prior_review"
        ),
        default=None,
    )
