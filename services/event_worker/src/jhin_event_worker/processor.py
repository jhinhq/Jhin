"""Message handling for the EVENTS stream consumer."""

from __future__ import annotations

import json
from collections import OrderedDict

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from pydantic import ValidationError

from jhin_events.envelope import EventEnvelope
from jhin_events.streams import EVENTS_STREAM
from jhin_events.subjects import dlq_subject
from jhin_observability import get_logger

logger = get_logger(__name__)


class EventProcessor:
    """Parses, dedupes (by event_id), logs, and acks consumed events.

    JetStream's duplicate window already dedupes republished messages
    server-side; this consumer-side set additionally makes *redelivery*
    (e.g. after a worker crash between processing and ack) effectively-once.
    """

    def __init__(self, js: JetStreamContext, *, max_remembered: int = 10_000) -> None:
        self._js = js
        self._max_remembered = max_remembered
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def handle(self, msg: Msg) -> None:
        try:
            envelope = EventEnvelope.from_bytes(msg.data)
        except ValidationError as exc:
            # Sanitized metadata only — never forward the raw payload blindly.
            await self._js.publish(
                dlq_subject(EVENTS_STREAM),
                json.dumps(
                    {
                        "reason": "invalid_envelope",
                        "subject": msg.subject,
                        "error_count": exc.error_count(),
                    }
                ).encode(),
            )
            logger.error("event.invalid_envelope", subject=msg.subject)
            await msg.term()
            return

        event_id = str(envelope.event_id)
        metadata = msg.metadata
        if event_id in self._seen:
            logger.info(
                "event.duplicate_skipped",
                event_id=event_id,
                subject=msg.subject,
                num_delivered=metadata.num_delivered,
            )
            await msg.ack()
            return

        self._seen[event_id] = None
        while len(self._seen) > self._max_remembered:
            self._seen.popitem(last=False)

        logger.info(
            "event.processed",
            event_id=event_id,
            event_type=envelope.event_type,
            workspace_id=envelope.workspace_id,
            subject=msg.subject,
            num_delivered=metadata.num_delivered,
            stream_seq=metadata.sequence.stream if metadata.sequence else None,
        )
        await msg.ack()
