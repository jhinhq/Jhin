"""Declarative base shared by all Jhin ORM models.

Alembic autogenerate diffs against this metadata. Never auto-create tables at
application startup — migrations are the only path to schema changes (plan
section 34).
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Fetch server-generated columns (created_at/updated_at) via RETURNING on
    # INSERT/UPDATE so async sessions never lazy-load them after commit.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
