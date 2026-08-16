"""Publishing helpers for the Jhin event backbone."""

from __future__ import annotations

from nats.js import JetStreamContext
from nats.js.api import PubAck

from jhin_events.envelope import EventEnvelope
from jhin_events.subjects import event_subject

MSG_ID_HEADER = "Nats-Msg-Id"


class EventPublisher:
    """Publishes canonical domain events with server-side deduplication.

    The envelope's ``event_id`` is attached as the JetStream message id, so a
    duplicate publish inside the stream's duplicate window is acknowledged but
    not stored again (``PubAck.duplicate`` is set).
    """

    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    async def publish(self, envelope: EventEnvelope) -> PubAck:
        subject = event_subject(envelope.workspace_id, envelope.event_type)
        return await self._js.publish(
            subject,
            envelope.to_bytes(),
            headers={MSG_ID_HEADER: str(envelope.event_id)},
        )
