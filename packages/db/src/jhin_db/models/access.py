"""Workspace access control: invitations and scoped API keys.

Both tables follow the same secret-handling rule as ``user_session``: only a
SHA-256 hash of the opaque token is stored, so a database leak cannot be
replayed (docs/architecture/rbac.md, docs/architecture/api-keys.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import (
    CreatedAtMixin,
    JsonList,
    StdUuid,
    TimestampMixin,
    UtcDateTime,
    UuidPkMixin,
)
from jhin_domain import WorkspaceRole


class WorkspaceInvitation(Base, UuidPkMixin, TimestampMixin):
    """A single-use, expiring invitation to join one workspace.

    There is no email dependency in Jhin: the plaintext token is returned to
    the inviting admin exactly once, as a URL to share out of band.
    """

    __tablename__ = "workspace_invitation"
    __table_args__ = (Index("ix_workspace_invitation_workspace_email", "workspace_id", "email"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(32), default=WorkspaceRole.MEMBER.value)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    invited_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    accepted_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class ApiKey(Base, UuidPkMixin, TimestampMixin):
    """A scoped, workspace-bound bearer credential.

    ``role_ceiling`` freezes the creator's role at creation time. Effective
    permission is always ``intersection(scopes_json, role_ceiling)`` — see
    :func:`jhin_domain.effective_scopes`.
    """

    __tablename__ = "api_key"

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    # Public identifier printed inside the key itself; used to find the row
    # before the constant-time secret comparison.
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    role_ceiling: Mapped[str] = mapped_column(String(32), default=WorkspaceRole.VIEWER.value)
    scopes_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class ApiKeyUsage(Base, UuidPkMixin, CreatedAtMixin):
    """One authenticated API-key request.

    A dedicated table rather than ``audit_event`` rows: this is high-volume
    request telemetry with its own retention and its own visibility rules,
    and mixing it into the append-only audit log would drown it.
    """

    __tablename__ = "api_key_usage"
    __table_args__ = (Index("ix_api_key_usage_workspace_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    api_key_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("api_key.id", ondelete="CASCADE"), index=True
    )
    # The human the key acts as; kept denormalized so usage survives key
    # deletion ordering and powers the per-role visibility filter cheaply.
    acting_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    method: Mapped[str] = mapped_column(String(8))
    # Route template only (``/api/v1/workspaces/{workspace_id}/agents``), never
    # the query string: query parameters can carry filter values.
    path: Mapped[str] = mapped_column(String(300))
    status_code: Mapped[int] = mapped_column(Integer)
    ip_hash: Mapped[str | None] = mapped_column(String(128), default=None)
