"""Reusable column types and mixins for Jhin models (plan section 6).

JSON columns use JSONB on Postgres but degrade to generic JSON elsewhere so
unit tests can run against SQLite without a running database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, Dialect, TypeDecorator, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jhin_domain import new_uuid7


class StdUuid(TypeDecorator[UUID]):
    """Native database UUID that always yields an exact stdlib ``uuid.UUID``.

    Drivers such as asyncpg return their own ``uuid.UUID`` subclass for
    native uuid columns. Several identity guards require the exact builtin
    type (they reject subclasses as potentially hostile), so normalization
    happens once, here, at the persistence boundary.
    """

    impl = Uuid
    cache_ok = True

    def __init__(self) -> None:
        super().__init__()
        self.impl_instance = Uuid(as_uuid=True)

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        return dialect.type_descriptor(Uuid(as_uuid=True))

    def process_bind_param(self, value: UUID | None, dialect: Dialect) -> UUID | None:
        if value is None or type(value) is UUID:
            return value
        return UUID(str(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> UUID | None:
        if value is None or type(value) is UUID:
            return value
        return UUID(str(value))


JsonDict = JSON().with_variant(JSONB(), "postgresql")
JsonList = JSON().with_variant(JSONB(), "postgresql")
UtcDateTime = DateTime(timezone=True)


class UuidPkMixin:
    """UUIDv7 primary key (time-ordered, index-friendly)."""

    id: Mapped[UUID] = mapped_column(StdUuid, primary_key=True, default=new_uuid7)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )


class TimestampMixin(CreatedAtMixin):
    """created_at everywhere; updated_at on mutable rows (plan section 6)."""

    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
