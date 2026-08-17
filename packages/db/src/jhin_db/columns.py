"""Reusable column types and mixins for Jhin models (plan section 6).

JSON columns use JSONB on Postgres but degrade to generic JSON elsewhere so
unit tests can run against SQLite without a running database.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jhin_domain import new_uuid7

JsonDict = JSON().with_variant(JSONB(), "postgresql")
JsonList = JSON().with_variant(JSONB(), "postgresql")
UtcDateTime = DateTime(timezone=True)


class UuidPkMixin:
    """UUIDv7 primary key (time-ordered, index-friendly)."""

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid7)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )


class TimestampMixin(CreatedAtMixin):
    """created_at everywhere; updated_at on mutable rows (plan section 6)."""

    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
