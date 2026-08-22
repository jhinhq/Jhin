"""Capability grants, approvals, and tool calls (plan 6.6, 6.15, 6.16).

These tables are the durable substance of the tool gateway: what an agent is
allowed to do (grants), what a human must decide (approvals), and what
actually happened (tool calls, always sanitized before persistence — never
bearer tokens, Authorization headers, private keys, cookies, or raw
connection strings in the JSON fields).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import (
    CreatedAtMixin,
    JsonDict,
    StdUuid,
    TimestampMixin,
    UtcDateTime,
    UuidPkMixin,
)
from jhin_domain import ApprovalStatus


class AgentCapabilityGrant(Base, UuidPkMixin, TimestampMixin):
    """One allow/deny statement for one agent (plan 6.6). Deny-by-default:
    absence of any matching allow row means the call is denied."""

    __tablename__ = "agent_capability_grant"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    capability: Mapped[str] = mapped_column(String(200))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    effect: Mapped[str] = mapped_column(String(16))  # allow | deny


class Approval(Base, UuidPkMixin, TimestampMixin):
    """A human decision request (plan 6.16). The row in Postgres — not any
    workflow signal or model output — is the authority on the decision."""

    __tablename__ = "approval"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("task.id", ondelete="SET NULL"), default=None, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="SET NULL"), default=None, index=True
    )
    requested_by_agent_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None, index=True
    )
    action_type: Mapped[str] = mapped_column(String(200))
    action_payload_sanitized: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default=ApprovalStatus.PENDING.value, index=True
    )
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )


class ToolCall(Base, UuidPkMixin, CreatedAtMixin):
    """One gateway-mediated tool call (plan 6.15), sanitized before insert."""

    __tablename__ = "tool_call"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent_run.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    # Connections arrive with the Phase 5 connector framework; no FK yet.
    connection_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    sanitized_input_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    sanitized_output_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True)
    approval_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("approval.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    error_code: Mapped[str | None] = mapped_column(String(100), default=None)
