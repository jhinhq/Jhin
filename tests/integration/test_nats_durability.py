"""Phase 1 exit test (c): JetStream events survive a consumer restart.

An event published while the event worker is stopped must be delivered after
it restarts (durable pull consumer), and re-publishing the same envelope must
be dropped server-side by the duplicate window (dedupe by event_id).
"""

from __future__ import annotations

import asyncio

import nats
import pytest
from nats.js import JetStreamContext

from jhin_events.envelope import EventEnvelope, EventSource
from jhin_events.publisher import EventPublisher
from jhin_events.streams import EVENTS_STREAM
from tests.integration.conftest import NATS_URL, compose

pytestmark = pytest.mark.integration

DURABLE = "event-worker"


async def _consumer_state(js: JetStreamContext) -> tuple[int, int]:
    info = await js.consumer_info(EVENTS_STREAM, DURABLE)
    return info.num_pending, info.delivered.consumer_seq


async def test_event_survives_consumer_restart_and_dedupes() -> None:
    client = await nats.connect(NATS_URL, connect_timeout=5)
    try:
        js = client.jetstream()
        publisher = EventPublisher(js)
        envelope = EventEnvelope(
            event_type="system.integration.test",
            workspace_id="ws-integration",
            source=EventSource(type="test"),
            data={"purpose": "phase1-exit-test"},
        )

        # Publish while the consumer is down.
        await asyncio.to_thread(compose, "stop", "event-worker")
        first_ack = await publisher.publish(envelope)
        assert not first_ack.duplicate

        # Restart; the durable consumer must receive and ack the message.
        await asyncio.to_thread(compose, "start", "event-worker")
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            num_pending, delivered = await _consumer_state(js)
            if num_pending == 0 and delivered >= first_ack.seq:
                break
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(
                    f"event not consumed after restart: pending={num_pending}, "
                    f"delivered_seq={delivered}, published_seq={first_ack.seq}"
                )
            await asyncio.sleep(1)

        # Re-publishing the same event_id inside the duplicate window is a no-op.
        duplicate_ack = await publisher.publish(envelope)
        assert duplicate_ack.duplicate
        assert duplicate_ack.seq == first_ack.seq
    finally:
        await client.close()
