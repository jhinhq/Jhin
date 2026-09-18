"""Scoped settings, encrypted chat captures, and approved credential consumers."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class ScopedVariable(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "scoped_variable"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scope", "scope_id", "name", name="uq_variable_namespace"),
        CheckConstraint("scope IN ('agent', 'team', 'company')", name="ck_variable_scope"),
        CheckConstraint(
            "(sensitive AND plaintext IS NULL AND secret_id IS NOT NULL) OR "
            "(NOT sensitive AND plaintext IS NOT NULL AND secret_id IS NULL)",
            name="ck_variable_storage",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID] = mapped_column(StdUuid, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    plaintext: Mapped[str | None] = mapped_column(Text, default=None)
    secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="RESTRICT"), unique=True, default=None
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Immutable provenance remains meaningful after the original is deleted.
    source_variable_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    source_version: Mapped[int | None] = mapped_column(Integer, default=None)
    created_by_type: Mapped[str] = mapped_column(String(16))
    created_by_id: Mapped[UUID] = mapped_column(StdUuid)
    updated_by_type: Mapped[str] = mapped_column(String(16))
    updated_by_id: Mapped[UUID] = mapped_column(StdUuid)


class SecureInputCapture(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "secure_input_capture"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "user_id",
            "fingerprint",
            name="uq_capture_chat_value",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(StdUuid, ForeignKey("user.id", ondelete="CASCADE"))
    secret_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="RESTRICT"), unique=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32), default="secret")
    variable_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("scoped_variable.id", ondelete="SET NULL"), default=None
    )
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class VariableConnectionBinding(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "variable_connection_binding"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "connection_id", "credential_field", name="uq_variable_connection_field"
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    variable_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("scoped_variable.id", ondelete="RESTRICT"), index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE"), index=True
    )
    credential_field: Mapped[str] = mapped_column(String(120))
    approved_origin: Mapped[str] = mapped_column(String(2048))
    approved_admin_url: Mapped[str] = mapped_column(String(2048), default="")
    created_by_agent_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("agent.id", ondelete="CASCADE")
    )
