"""Bounded memoization of public catalog identities for immutable generations.

The engine, rather than its URL, identifies the database: different in-memory
databases and independently configured engines must never share results. Weak
keys let disposed engines disappear without retaining their pools or sessions.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_GENERATIONS = 2
_Compute = Callable[[AsyncSession, UUID], Awaitable[tuple[str, ...]]]


@dataclass
class _EngineIdentities:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generations: OrderedDict[UUID, tuple[str, ...]] = field(default_factory=OrderedDict)


_engines: WeakKeyDictionary[Engine, _EngineIdentities] = WeakKeyDictionary()


async def cached_duplicate_keys(
    db: AsyncSession, version_id: UUID, compute: _Compute
) -> tuple[str, ...]:
    """Compute once per engine/generation, including concurrent cold readers.

    Only a successfully completed tuple is retained. Cancellation or failure
    releases the lock and lets the next request retry using its own session.
    Callers still resolve the active generation on every request; a swap uses
    a new UUID immediately, without a TTL or external invalidation protocol.
    """
    engine = db.get_bind().engine
    cache = _engines.get(engine)
    if cache is None:
        cache = _EngineIdentities()
        _engines[engine] = cache
    async with cache.lock:
        if version_id in cache.generations:
            cache.generations.move_to_end(version_id)
            return cache.generations[version_id]
        result = await compute(db, version_id)
        cache.generations[version_id] = result
        while len(cache.generations) > _MAX_GENERATIONS:
            cache.generations.popitem(last=False)
        return result
