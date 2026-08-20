"""Message handling for the EVENTS stream consumer."""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Protocol

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from pydantic import ValidationError

from jhin_events.envelope import EventEnvelope
from jhin_events.streams import EVENTS_STREAM
from jhin_events.subjects import dlq_subject
from jhin_observability import SafeErrorCode, get_logger, normalize_event_family

logger = get_logger(__name__)


class EventHandler(Protocol):
    """Downstream processing hook (the TriggerMatcher in production)."""

    async def handle_event(self, envelope: EventEnvelope) -> None: ...


class EventProcessor:
    """Parses, dedupes (by event_id), dispatches, and acks consumed events.

    JetStream's duplicate window already dedupes republished messages
    server-side; this consumer-side set additionally makes *redelivery*
    (e.g. after a worker crash between processing and ack) effectively-once.
    """

    def __init__(
        self,
        js: JetStreamContext,
        *,
        matcher: EventHandler | None = None,
        max_remembered: int = 10_000,
    ) -> None:
        self._js = js
        self._matcher = matcher
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
            logger.error(
                "event.invalid_envelope",
                error_code=SafeErrorCode.INVALID_REQUEST.value,
            )
            await msg.term()
            return

        event_id = str(envelope.event_id)
        metadata = msg.metadata
        if event_id in self._seen:
            logger.info(
                "event.duplicate_skipped",
                num_delivered=metadata.num_delivered,
            )
            await msg.ack()
            return

        # Dispatch before remembering: a matcher failure propagates so the
        # consumer naks for redelivery, and the retry is not skipped as seen.
        if self._matcher is not None:
            await self._matcher.handle_event(envelope)

        self._seen[event_id] = None
        while len(self._seen) > self._max_remembered:
            self._seen.popitem(last=False)

        logger.info(
            "event.processed",
            event_type=normalize_event_family(envelope.event_type),
            num_delivered=metadata.num_delivered,
        )
        await msg.ack()
