"""Dependency connectivity checks backing the readiness endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter

import nats
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.exceptions import CancelledError as TemporalCancelledError

from jhin_api.health.schemas import DependencyStatus, ReadinessReport
from jhin_api.settings import Settings
from jhin_api.temporal import TemporalClientProvider

CHECK_TIMEOUT_SECONDS = 5.0


async def check_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_nats(nats_url: str) -> None:
    client = await nats.connect(nats_url, connect_timeout=3, allow_reconnect=False)
    try:
        # Verifies JetStream is enabled, not just that the socket accepts us.
        await client.jetstream().account_info()
    finally:
        await client.close()


class TemporalHealthUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Temporal workflow service is unavailable")


async def check_temporal(provider: TemporalClientProvider) -> None:
    try:
        client = await provider.get()
        healthy = await client.service_client.check_health()
    except (asyncio.CancelledError, TemporalCancelledError):
        raise
    except Exception:
        raise TemporalHealthUnavailable() from None
    if not healthy:
        raise TemporalHealthUnavailable()


async def _timed(name: str, check: Callable[[], Awaitable[None]]) -> DependencyStatus:
    start = perf_counter()
    try:
        await asyncio.wait_for(check(), CHECK_TIMEOUT_SECONDS)
    except (asyncio.CancelledError, TemporalCancelledError):
        raise
    except Exception as exc:
        return DependencyStatus(
            name=name,
            status="error",
            latency_ms=round((perf_counter() - start) * 1000, 2),
            detail=f"{type(exc).__name__}: {exc}"[:300],
        )
    return DependencyStatus(
        name=name, status="ok", latency_ms=round((perf_counter() - start) * 1000, 2)
    )


async def readiness(
    settings: Settings,
    engine: AsyncEngine,
    temporal_provider: TemporalClientProvider,
) -> ReadinessReport:
    dependencies = await asyncio.gather(
        _timed("postgres", lambda: check_postgres(engine)),
        _timed("nats", lambda: check_nats(settings.nats_url)),
        _timed(
            "temporal",
            lambda: check_temporal(temporal_provider),
        ),
    )
    status = "ok" if all(dep.status == "ok" for dep in dependencies) else "degraded"
    return ReadinessReport(status=status, app=settings.app_name, dependencies=list(dependencies))
