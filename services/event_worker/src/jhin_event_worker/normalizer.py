"""INGRESS stream consumer: normalize raw connector events (plan 9.4, 11).

Each accepted webhook delivery arrives here as one ingress envelope. The
owning connector maps it to zero or more canonical ``connector.*`` events,
published to the EVENTS stream. Normalized event ids are *derived* (UUIDv5
of the ingress event id + index), so a redelivery after a crash between
publish and ack regenerates identical ids and JetStream's duplicate window
drops the copies — duplicate deliveries never duplicate events (plan 48.6).
"""

from __future__ import annotations

import json
import uuid
from uuid import UUID

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from pydantic import ValidationError

from jhin_connectors import ConnectorRegistry, RawWebhookEvent, default_registry
from jhin_events.envelope import EventEnvelope
from jhin_events.publisher import EventPublisher
from jhin_events.streams import INGRESS_STREAM
from jhin_events.subjects import dlq_subject
from jhin_observability import (
    SafeErrorCode,
    get_logger,
    normalize_connector_type,
    normalize_event_family,
)

logger = get_logger(__name__)

# Namespace for deriving normalized event ids from ingress event ids.
_NORMALIZED_NS = uuid.UUID("6a1de7a4-52a5-4a3e-9a3d-1fca0f6b8f11")


def derived_event_id(ingress_event_id: UUID, index: int) -> UUID:
    return uuid.uuid5(_NORMALIZED_NS, f"{ingress_event_id}:{index}")


class IngressNormalizer:
    """Handler for the INGRESS pull consumer."""

    def __init__(self, js: JetStreamContext, registry: ConnectorRegistry | None = None) -> None:
        self._js = js
        self._publisher = EventPublisher(js)
        self._registry = registry if registry is not None else default_registry()

    async def handle(self, msg: Msg) -> None:
        try:
            envelope = EventEnvelope.from_bytes(msg.data)
        except ValidationError as exc:
            await self._js.publish(
                dlq_subject(INGRESS_STREAM),
                json.dumps(
                    {
                        "reason": "invalid_envelope",
                        "subject": msg.subject,
                        "error_count": exc.error_count(),
                    }
                ).encode(),
            )
            logger.error(
                "ingress.invalid_envelope",
                error_code=SafeErrorCode.INVALID_REQUEST.value,
            )
            await msg.term()
            return

        connector = self._registry.get(envelope.source.type)
        event_name = str(envelope.data.get("event", ""))
        payload = envelope.data.get("payload")
        if connector is None or not event_name or not isinstance(payload, dict):
            # Nothing can ever normalize this; drop it without redelivery.
            logger.warning(
                "ingress.unhandled",
                connector_type=normalize_connector_type(envelope.source.type),
                event_type=normalize_event_family(event_name),
            )
            await msg.term()
            return

        raw = RawWebhookEvent(
            event=event_name,
            delivery_id=str(envelope.data.get("delivery_id", "")),
            payload=payload,
        )
        normalized = connector.normalize_event(raw)
        for index, item in enumerate(normalized):
            await self._publisher.publish(
                EventEnvelope(
                    event_id=derived_event_id(envelope.event_id, index),
                    event_type=item.event_type,
                    workspace_id=envelope.workspace_id,
                    correlation_id=envelope.correlation_id,
                    causation_id=envelope.event_id,
                    source=envelope.source,
                    data=item.data,
                )
            )
        logger.info(
            "ingress.normalized",
            connector_type=normalize_connector_type(envelope.source.type),
            event_type=normalize_event_family(event_name),
            produced=len(normalized),
        )
        await msg.ack()
