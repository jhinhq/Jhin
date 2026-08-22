"""Append-only audit trail (plan sections 6.17 and 23).

No UPDATE or DELETE code path may touch this table. ``workspace_id`` is a
plain UUID (no foreign key) so audit history survives workspace deletion.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, JsonDict, StdUuid, UuidPkMixin


class AuditEvent(Base, UuidPkMixin, CreatedAtMixin):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_workspace_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    request_id: Mapped[UUID | None] = mapped_column(StdUuid, default=None)
    ip_hash: Mapped[str | None] = mapped_column(String(128), default=None)
