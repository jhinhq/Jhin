"""Generation identity caching never shares database state or failed work."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from jhin_api.catalog.identity_cache import cached_duplicate_keys


async def test_same_version_uuid_on_different_engines_does_not_share_results() -> None:
    engines = [create_async_engine("sqlite+aiosqlite://") for _ in range(2)]
    version = uuid4()
    calls = 0

    async def compute(db: AsyncSession, _: UUID) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return (str(id(db.get_bind().engine)),)

    try:
        async with AsyncSession(engines[0]) as first, AsyncSession(engines[1]) as second:
            a = await cached_duplicate_keys(first, version, compute)
            b = await cached_duplicate_keys(second, version, compute)
            assert a != b
            assert await cached_duplicate_keys(first, version, compute) == a
            assert calls == 2
    finally:
        for engine in engines:
            await engine.dispose()


async def test_concurrent_sessions_coalesce_one_cold_generation_computation() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    version = uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def compute(_: AsyncSession, __: UUID) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ("same-product",)

    try:
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            pending = asyncio.create_task(cached_duplicate_keys(first, version, compute))
            await entered.wait()
            follower = asyncio.create_task(cached_duplicate_keys(second, version, compute))
            await asyncio.sleep(0)
            assert calls == 1
            release.set()
            assert await pending == await follower == ("same-product",)
            assert calls == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_computation_does_not_poison_the_generation(
    session: AsyncSession, failure: type[BaseException]
) -> None:
    version = uuid4()
    calls = 0

    async def compute(_: AsyncSession, __: UUID) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure()
        return ("recovered",)

    with pytest.raises(failure):
        await cached_duplicate_keys(session, version, compute)
    assert await cached_duplicate_keys(session, version, compute) == ("recovered",)
    assert await cached_duplicate_keys(session, version, compute) == ("recovered",)
    assert calls == 2


async def test_completed_generations_are_bounded_and_evicted_work_is_recomputed(
    session: AsyncSession,
) -> None:
    calls = 0

    async def compute(_: AsyncSession, version: UUID) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return (str(version),)

    versions = [uuid4() for _ in range(3)]
    for version in versions:
        await cached_duplicate_keys(session, version, compute)
    await cached_duplicate_keys(session, versions[-1], compute)
    assert calls == 3
    await cached_duplicate_keys(session, versions[0], compute)
    assert calls == 4


async def test_cancelling_a_waiter_does_not_cancel_the_active_computation(
    session: AsyncSession,
) -> None:
    version = uuid4()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def compute(_: AsyncSession, __: UUID) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ("complete",)

    pending = asyncio.create_task(cached_duplicate_keys(session, version, compute))
    await entered.wait()
    waiter = asyncio.create_task(cached_duplicate_keys(session, version, compute))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await pending == ("complete",)
    assert await cached_duplicate_keys(session, version, compute) == ("complete",)
    assert calls == 1
