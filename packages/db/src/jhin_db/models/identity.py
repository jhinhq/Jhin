"""Users and server-side sessions (plan sections 6.2 and 20.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import CreatedAtMixin, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import UserStatus


class User(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "user"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default=UserStatus.ACTIVE.value)


class UserSession(Base, UuidPkMixin, CreatedAtMixin):
    """Server-side session record.

    Only a SHA-256 hash of the opaque bearer token is stored; the plaintext
    token exists solely in the user's cookie (plan 20.1, security invariants).
    """

    __tablename__ = "user_session"

    user_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    ip_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(400), default=None)
