"""Coordination and oversight records: peer work requests, review policies,
and work reviews (migration ``0018``).

A ``work_request`` is a durable, idempotent ask between peers or across
teams. It is deliberately distinct from delegation: an accepted request
creates exactly one standalone task (``parent_task_id`` stays NULL) linked
only through ``created_task_id`` and task metadata, so ownership never
transfers implicitly.

A ``review_policy`` configures exception-based oversight; each triggered
review becomes one ``work_review`` keyed by a deterministic trigger key so
one exception yields at most one review. Reviews only gate and record: they
never substitute for a human security approval nor override tool policy.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, JsonList, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import ReviewMode, ReviewScopeKind, WorkRequestStatus, WorkReviewStatus


class WorkRequest(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "work_request"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_work_request_workspace_idempotency"
        ),
        UniqueConstraint("created_task_id", name="uq_work_request_created_task"),
        CheckConstraint("requester_agent_id <> target_agent_id", name="requester_not_target"),
        CheckConstraint("depth >= 1", name="depth_positive"),
        Index(
            "ix_work_request_workspace_target_status",
            "workspace_id",
            "target_agent_id",
            "status",
        ),
        Index("ix_work_request_workspace_requester", "workspace_id", "requester_agent_id"),
        Index("ix_work_request_workspace_root_task", "workspace_id", "root_task_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="SET NULL"), default=None
    )
    requester_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE")
    )
    requester_task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    requester_run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )
    # Top of the requester task's lineage (delegation parents and earlier
    # request hops). The ping-pong guard keys on it.
    root_task_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    # Set when a human opened the request on behalf of the requesting agent.
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    target_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    expected_output: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(
        String(32), default=WorkRequestStatus.PENDING.value, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    # 1 for a request opened from ordinary work; n+1 when opened from a task
    # that itself came from a request at depth n.
    depth: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    response: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    responded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)


class ReviewPolicy(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "review_policy"
    __table_args__ = (
        CheckConstraint(
            "(scope_kind = 'workspace' AND scope_id IS NULL AND scope_key IS NULL) OR "
            "(scope_kind IN ('team', 'agent') AND scope_id IS NOT NULL AND scope_key IS NULL) OR "
            "(scope_kind = 'task_type' AND scope_id IS NULL AND scope_key IS NOT NULL)",
            name="scope_shape",
        ),
        CheckConstraint(
            "mode IN ('pre_action', 'before_close', 'post_action', 'periodic')", name="mode"
        ),
        Index("ix_review_policy_workspace_enabled", "workspace_id", "enabled"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    scope_kind: Mapped[str] = mapped_column(String(32), default=ReviewScopeKind.WORKSPACE.value)
    scope_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    scope_key: Mapped[str | None] = mapped_column(String(100), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    mode: Mapped[str] = mapped_column(String(32), default=ReviewMode.BEFORE_CLOSE.value)
    conditions_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    reviewer_selector_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    # Mandatory: a blocking-mode review with no resolvable reviewer fails
    # closed instead of being skipped.
    fail_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    # Periodic mode cadence; ignored for other modes.
    period_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )


class WorkReview(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "work_review"
    __table_args__ = (
        UniqueConstraint("workspace_id", "trigger_key", name="uq_work_review_workspace_trigger"),
        CheckConstraint(
            "(reviewer_type = 'agent' AND reviewer_agent_id IS NOT NULL "
            "AND reviewer_user_id IS NULL) OR "
            "(reviewer_type = 'human' AND reviewer_agent_id IS NULL) OR "
            "(reviewer_type = 'none' AND reviewer_agent_id IS NULL AND reviewer_user_id IS NULL)",
            name="reviewer_shape",
        ),
        Index("ix_work_review_workspace_status", "workspace_id", "status"),
        Index("ix_work_review_workspace_reviewer_agent", "workspace_id", "reviewer_agent_id"),
        Index("ix_work_review_workspace_task", "workspace_id", "task_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("review_policy.id", ondelete="SET NULL"), default=None
    )
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None
    )
    run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    work_request_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("work_request.id", ondelete="SET NULL"), default=None
    )
    # Agent whose work is under review (the source run's agent).
    subject_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    trigger_key: Mapped[str] = mapped_column(String(300))
    mode: Mapped[str] = mapped_column(String(32))
    # Source evidence references (tool name/risk, cost, failure codes,
    # artifacts, message ids). Sanitized structure only — never transcripts.
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    # "agent" | "human" | "none" (skipped/fail-closed with no reviewer).
    reviewer_type: Mapped[str] = mapped_column(String(16))
    reviewer_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    reviewer_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(
        String(32), default=WorkReviewStatus.PENDING.value, index=True
    )
    verdict: Mapped[str | None] = mapped_column(String(32), default=None)
    feedback: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    decided_by_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
