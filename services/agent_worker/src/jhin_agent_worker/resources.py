"""Shared long-lived resources for agent activities.

One engine, one NATS connection, one master-key load per process. Activities
receive this container instead of building clients per call.
"""

from __future__ import annotations

from dataclasses import dataclass

import nats
from nats.aio.client import Client as NatsClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_agent_worker.settings import Settings
from jhin_db import create_engine, create_session_factory
from jhin_events import EventPublisher
from jhin_events.streams import ensure_streams
from jhin_observability import get_logger
from jhin_secrets import SecretCrypto, load_master_key

logger = get_logger(__name__)


@dataclass
class Resources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    nats_connection: NatsClient
    publisher: EventPublisher
    crypto: SecretCrypto

    @classmethod
    async def create(cls, settings: Settings) -> Resources:
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        nats_connection = await nats.connect(settings.nats_url)
        js = nats_connection.jetstream()
        await ensure_streams(js)
        crypto = SecretCrypto(load_master_key())
        logger.info("resources.ready", nats_url=settings.nats_url)
        return cls(
            engine=engine,
            session_factory=session_factory,
            nats_connection=nats_connection,
            publisher=EventPublisher(js),
            crypto=crypto,
        )

    async def close(self) -> None:
        await self.nats_connection.drain()
        await self.engine.dispose()
