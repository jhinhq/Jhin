"""Async SQLAlchemy engine/session construction."""

from __future__ import annotations

from contextlib import suppress

from opentelemetry.trace import Tracer
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jhin_db.base import Base
from jhin_observability import noop_tracer
from jhin_observability.sqlalchemy import install_sqlalchemy_tracing


def create_engine(
    database_url: str,
    *,
    trace_sql: bool = True,
    tracer: Tracer | None = None,
) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    if trace_sql:
        with suppress(Exception):
            install_sqlalchemy_tracing(
                engine.sync_engine,
                frozenset(Base.metadata.tables),
                tracer=tracer if tracer is not None else noop_tracer(),
            )
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
