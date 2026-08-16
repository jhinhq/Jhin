"""Declarative base shared by all Jhin ORM models.

Domain tables arrive in Phase 2; Alembic autogenerate diffs against this
metadata. Never auto-create tables at application startup — migrations are the
only path to schema changes (plan section 34).
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
