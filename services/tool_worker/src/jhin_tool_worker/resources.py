"""Long-lived database, event, secret, and crash-barrier resources."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import nats
from nats.aio.client import Client as NatsClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_db import create_engine, create_session_factory
from jhin_events import EventPublisher
from jhin_events.streams import ensure_streams
from jhin_secrets import SecretCrypto, load_master_key
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tools import CrashBarrier, CrashBarrierConfig

logger = logging.getLogger(__name__)


@dataclass
class ToolWorkerResources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    nats_connection: NatsClient
    publisher: EventPublisher
    crypto: SecretCrypto
    test_barrier: CrashBarrier

    @classmethod
    async def create(cls, settings: ToolWorkerSettings) -> ToolWorkerResources:
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        nats_connection = await nats.connect(settings.nats_url)
        jetstream = nats_connection.jetstream()
        await ensure_streams(jetstream)
        resources = cls(
            engine=engine,
            session_factory=session_factory,
            nats_connection=nats_connection,
            publisher=EventPublisher(jetstream),
            crypto=SecretCrypto(load_master_key()),
            test_barrier=CrashBarrier(
                CrashBarrierConfig(
                    root=settings.test_crash_barrier_dir,
                    selected=settings.test_crash_barrier_name,
                    match_identity=settings.test_crash_barrier_match,
                )
            ),
        )
        logger.info("tool worker resources ready")
        return resources

    async def close(self) -> None:
        await self.nats_connection.drain()
        await self.engine.dispose()


__all__ = ["ToolWorkerResources"]
