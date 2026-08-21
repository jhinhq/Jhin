"""Shared long-lived resources for agent activities.

One engine, one NATS connection, one master-key load per process. Activities
receive this container instead of building clients per call.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from types import TracebackType

import nats
from nats.aio.client import Client as NatsClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_agent_worker.settings import Settings
from jhin_db import create_engine, create_session_factory
from jhin_events import EventPublisher
from jhin_events.streams import ensure_streams
from jhin_observability import ObservabilityRuntime, get_logger
from jhin_secrets import SecretCrypto, load_master_key
from jhin_tools import CrashBarrier, CrashBarrierConfig

logger = get_logger(__name__)


@dataclass
class Resources:
    runtime: ObservabilityRuntime
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    nats_connection: NatsClient
    publisher: EventPublisher
    crypto: SecretCrypto
    test_barrier: CrashBarrier

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        runtime: ObservabilityRuntime,
    ) -> Resources:
        engine = create_engine(settings.database_url, tracer=runtime.tracer)
        nats_connection: NatsClient | None = None
        try:
            session_factory = create_session_factory(engine)
            nats_connection = await nats.connect(settings.nats_url)
            js = nats_connection.jetstream()
            await ensure_streams(js)
            crypto = SecretCrypto(load_master_key())
            test_barrier = CrashBarrier(
                CrashBarrierConfig(
                    root=settings.test_crash_barrier_dir,
                    selected=settings.test_crash_barrier_name,
                    match_identity=settings.test_crash_barrier_match,
                )
            )
            resources = cls(
                runtime=runtime,
                engine=engine,
                session_factory=session_factory,
                nats_connection=nats_connection,
                publisher=EventPublisher(js, tracer=runtime.tracer),
                crypto=crypto,
                test_barrier=test_barrier,
            )
            logger.info("resources.ready")
            return resources
        except BaseException:
            if nats_connection is not None:
                with contextlib.suppress(BaseException):
                    await nats_connection.drain()
            with contextlib.suppress(BaseException):
                await engine.dispose()
            raise

    async def close(self) -> None:
        cancellation: asyncio.CancelledError | None = None
        cancellation_traceback: TracebackType | None = None
        error: BaseException | None = None
        error_traceback: TracebackType | None = None

        def remember(exc: BaseException) -> None:
            nonlocal cancellation, cancellation_traceback, error, error_traceback
            if isinstance(exc, asyncio.CancelledError):
                if cancellation is None:
                    cancellation = exc
                    cancellation_traceback = exc.__traceback__
            elif error is None:
                error = exc
                error_traceback = exc.__traceback__

        try:
            await self.nats_connection.drain()
        except BaseException as exc:
            remember(exc)
        try:
            await self.engine.dispose()
        except BaseException as exc:
            remember(exc)
        if cancellation is not None:
            raise cancellation.with_traceback(cancellation_traceback)
        if error is not None:
            raise error.with_traceback(error_traceback)
