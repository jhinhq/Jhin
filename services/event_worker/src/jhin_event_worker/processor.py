"""Message handling for the EVENTS stream consumer."""

from __future__ import annotations

from collections import OrderedDict
from typing import Protocol

from nats.aio.msg import Msg
from opentelemetry.trace import Tracer
from pydantic import ValidationError

from jhin_events.envelope import EventEnvelope
from jhin_events.streams import EVENTS_STREAM
from jhin_events.telemetry import JetStreamPublisher, publish_invalid_envelope_dlq
from jhin_observability import (
    SafeErrorCode,
    bind_context,
    get_logger,
    is_safe_context_id,
    noop_tracer,
    normalize_event_family,
)

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
        js: JetStreamPublisher,
        *,
        matcher: EventHandler | None = None,
        max_remembered: int = 10_000,
        tracer: Tracer | None = None,
    ) -> None:
        self._tracer = tracer if tracer is not None else noop_tracer()
        self._js = js
        self._matcher = matcher
        self._max_remembered = max_remembered
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def handle(self, msg: Msg) -> None:
        try:
            envelope = EventEnvelope.from_bytes(msg.data)
        except ValidationError as exc:
            # Sanitized metadata only — never forward the raw payload blindly.
            await publish_invalid_envelope_dlq(
                self._js,
                origin_stream=EVENTS_STREAM,
                error_count=exc.error_count(),
                tracer=self._tracer,
            )
            logger.error(
                "event.invalid_envelope",
                error_code=SafeErrorCode.INVALID_REQUEST.value,
            )
            await msg.term()
            return

        if is_safe_context_id(envelope.workspace_id):
            with bind_context(
                workspace_id=envelope.workspace_id,
                correlation_id=envelope.correlation_id,
            ):
                await self._handle_valid(msg, envelope)
        else:
            with bind_context(correlation_id=envelope.correlation_id):
                await self._handle_valid(msg, envelope)

    async def _handle_valid(self, msg: Msg, envelope: EventEnvelope) -> None:
        """Process one schema-valid envelope under its safe diagnostic context."""

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
