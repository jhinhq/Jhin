"""pgvector helpers for the optional ``memory_record.embedding_vec`` column.

The column is created by migration 0016 only when the ``vector`` extension
is available, and is never ORM-mapped. Every helper here is a no-op on other
dialects or when the column is absent, so callers never need to branch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession

_availability: dict[str, bool] = {}


async def pgvector_available(session: AsyncSession) -> bool:
    """True when we are on PostgreSQL and ``embedding_vec`` exists."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    engine = bind if isinstance(bind, Engine) else bind.engine
    key = str(engine.url)
    cached = _availability.get(key)
    if cached is not None:
        return cached
    try:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'memory_record' AND column_name = 'embedding_vec'"
                )
            )
        ).first()
        available = row is not None
    except Exception:
        available = False
    _availability[key] = available
    return available


def reset_availability_cache() -> None:
    _availability.clear()


async def store_embedding_vector(
    session: AsyncSession, record_id: UUID, vector: Sequence[float] | None
) -> None:
    """Mirror the JSON embedding into the pgvector column when it exists."""
    if not await pgvector_available(session):
        return
    literal = None if vector is None else json.dumps([float(v) for v in vector])
    await session.execute(
        text("UPDATE memory_record SET embedding_vec = CAST(:vec AS vector) WHERE id = :id"),
        {"vec": literal, "id": record_id},
    )


async def nearest_record_ids(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    query: Sequence[float],
    limit: int,
) -> list[UUID]:
    """Approximate-nearest prefilter using the pgvector column; empty when
    unavailable (the caller falls back to recency/lexical candidates)."""
    if not await pgvector_available(session):
        return []
    rows = await session.execute(
        text(
            "SELECT id FROM memory_record "
            "WHERE workspace_id = :ws AND embedding_vec IS NOT NULL "
            "ORDER BY embedding_vec <=> CAST(:vec AS vector) LIMIT :limit"
        ),
        {"ws": workspace_id, "vec": json.dumps([float(v) for v in query]), "limit": limit},
    )
    return [row[0] for row in rows]
