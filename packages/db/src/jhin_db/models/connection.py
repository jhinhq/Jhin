"""Authenticated integration instances and webhook delivery dedupe
(plan 6.9, 9.4, 19).

A connection references its credentials and webhook signing secret by
``secret`` row id — plaintext never lives in this table. ``public_id`` is a
random URL token for the webhook endpoint so internal row ids are never
exposed in third-party configuration.
"""

from __future__ import annotations

import secrets as stdlib_secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
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
from jhin_domain import ConnectionStatus


def new_public_id() -> str:
    """URL-safe random token for webhook paths (128 bits)."""
    return stdlib_secrets.token_hex(16)


class Connection(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "connection"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    connector_type: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(200))
    auth_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=ConnectionStatus.ACTIVE.value)
    public_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=new_public_id
    )
    encrypted_secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    webhook_secret_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("secret.id", ondelete="SET NULL"), default=None
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class WebhookDelivery(Base, UuidPkMixin, CreatedAtMixin):
    """Processed webhook delivery ids (plan 9.4): a redelivered webhook with
    the same provider delivery id must never publish a second ingress event.
    The unique constraint is the authority; JetStream msg-id dedupe is the
    second line of defense."""

    __tablename__ = "webhook_delivery"
    __table_args__ = (UniqueConstraint("connection_id", "delivery_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="CASCADE"), index=True
    )
    delivery_id: Mapped[str] = mapped_column(String(200))
    event: Mapped[str] = mapped_column(String(100))
    # The ingress envelope id published for this delivery (deterministic).
    event_id: Mapped[UUID] = mapped_column(StdUuid)
