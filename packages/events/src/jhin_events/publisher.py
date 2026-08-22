"""Publishing helpers for the Jhin event backbone."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from nats.js.api import PubAck
from opentelemetry.trace import Tracer

from jhin_events.envelope import EventEnvelope
from jhin_events.subjects import event_subject
from jhin_events.telemetry import MSG_ID_HEADER as MSG_ID_HEADER
from jhin_events.telemetry import JetStreamPublisher, publish_jetstream


class EventPublisher:
    """Publishes canonical domain events with server-side deduplication.

    The envelope's ``event_id`` is attached as the JetStream message id, so a
    duplicate publish inside the stream's duplicate window is acknowledged but
    not stored again (``PubAck.duplicate`` is set).
    """

    def __init__(self, js: object, *, tracer: Tracer | None = None) -> None:
        from jhin_observability import noop_tracer

        self._js = cast(JetStreamPublisher, js)
        self._tracer = tracer if tracer is not None else noop_tracer()

    async def publish(
        self,
        envelope: EventEnvelope,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> PubAck:
        subject = event_subject(envelope.workspace_id, envelope.event_type)
        return await publish_jetstream(
            self._js,
            subject,
            envelope.to_bytes(),
            headers=headers,
            message_id=str(envelope.event_id),
            stream="EVENTS",
            tracer=self._tracer,
        )
