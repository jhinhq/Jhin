"""Webhook ingress processing (plan 19, 9.4, 48.5, 48.6).

Order of operations is a security invariant:

1. connection lookup by unguessable public id (128-bit random path token);
2. HMAC signature verification against the per-connection webhook secret —
   before the body is parsed or any state changes;
3. delivery-id dedupe via the ``webhook_delivery`` unique constraint;
4. raw event published to the INGRESS stream (normalization happens in the
   event worker, plan 9.4).

Rejections are audited with actor_type=system; accepted deliveries are
traceable through their ``webhook_delivery`` row and ingress event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid5

from fastapi import HTTPException, status
from nats.js import JetStreamContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_connectors import RawWebhookEvent, WebhookVerificationError, default_registry
from jhin_db.models import Connection, WebhookDelivery
from jhin_domain import ActorType, ConnectionStatus
from jhin_events.envelope import EventEnvelope, EventSource
from jhin_events.publisher import MSG_ID_HEADER
from jhin_events.subjects import ingress_subject
from jhin_observability import get_logger
from jhin_secrets import SecretCrypto, SecretStore

logger = get_logger(__name__)

MAX_WEBHOOK_BODY_BYTES = 1_048_576
INGRESS_EVENT_ID_NAMESPACE = UUID("65c0e8a1-4264-5f57-bd7c-bc170fdde583")


def ingress_event_id(connector_type: str, connection_id: UUID, delivery_id: str) -> UUID:
    """Stable ingress identity for retries of one provider delivery."""
    canonical_tuple = json.dumps(
        [connector_type, str(connection_id), delivery_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid5(INGRESS_EVENT_ID_NAMESPACE, canonical_tuple)


@dataclass(frozen=True)
class WebhookResult:
    """Outcome for the provider: accepted, duplicate (already processed), or
    ignored (verified but not an event this connector ingests, e.g. ping)."""

    outcome: str
    event_id: UUID | None = None


async def process_delivery(
    db: AsyncSession,
    crypto: SecretCrypto,
    js: JetStreamContext,
    *,
    connector_type: str,
    public_id: str,
    headers: Mapping[str, str],
    body: bytes,
    request_id: UUID,
    ip_hash: str,
) -> WebhookResult:
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook body is too large",
        )
    connector = default_registry().get(connector_type)
    if connector is None or not connector.manifest.supports_webhooks:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    connection = await db.scalar(
        select(Connection).where(
            Connection.public_id == public_id, Connection.connector_type == connector_type
        )
    )
    if connection is None or connection.status == ConnectionStatus.DISABLED.value:
        # Same 404 for unknown and disabled: the path token is the only
        # authentication, so nothing about connection state may leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if connection.webhook_secret_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    secret = await SecretStore(db, crypto).reveal(
        connection.workspace_id, connection.webhook_secret_id
    )

    try:
        raw = connector.parse_webhook(headers, body, secret)
    except WebhookVerificationError:
        audit.record(
            db,
            action="webhook.rejected",
            target_type="connection",
            target_id=connection.id,
            workspace_id=connection.workspace_id,
            actor_type=ActorType.SYSTEM,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"connector_type": connector_type, "reason": "verification_failed"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature verification failed"
        ) from None

    if raw.event not in connector.webhook_events():
        # Verified but not ingested (e.g. GitHub's "ping"): acknowledge so the
        # provider doesn't retry, publish nothing.
        return WebhookResult(outcome="ignored")

    return await _ingest(db, js, connection, raw)


async def _ingest(
    db: AsyncSession, js: JetStreamContext, connection: Connection, raw: RawWebhookEvent
) -> WebhookResult:
    event_id = ingress_event_id(connection.connector_type, connection.id, raw.delivery_id)
    delivery = WebhookDelivery(
        workspace_id=connection.workspace_id,
        connection_id=connection.id,
        delivery_id=raw.delivery_id,
        event=raw.event,
        event_id=event_id,
    )
    db.add(delivery)
    try:
        await db.flush()
    except IntegrityError:
        # Unique (connection_id, delivery_id): this delivery was already
        # processed — never publish a second event (plan 48.6).
        await db.rollback()
        return WebhookResult(outcome="duplicate")

    envelope = EventEnvelope(
        event_id=event_id,
        event_type=f"ingress.{connection.connector_type}.{raw.event}",
        workspace_id=str(connection.workspace_id),
        source=EventSource(type=connection.connector_type, connection_id=connection.id),
        data={"event": raw.event, "delivery_id": raw.delivery_id, "payload": raw.payload},
    )
    subject = ingress_subject(str(connection.workspace_id), connection.connector_type, raw.event)
    connection_id = str(connection.id)  # read before any rollback expires the row
    # Publish before commit: if NATS is down the row rolls back and the
    # provider's retry re-processes cleanly; JetStream's duplicate window
    # (keyed on event_id) covers the crash-after-publish edge.
    try:
        await js.publish(subject, envelope.to_bytes(), headers={MSG_ID_HEADER: str(event_id)})
        await db.commit()
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            logger.error(
                "webhook.rollback_failed",
                connection_id=connection_id,
                event_name=raw.event,
            )
        logger.error(
            "webhook.publish_or_commit_failed",
            connection_id=connection_id,
            event_name=raw.event,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event backbone is unavailable; retry later",
        ) from None
    logger.info(
        "webhook.accepted",
        connection_id=connection_id,
        event_name=raw.event,
        delivery_id=raw.delivery_id,
        event_id=str(event_id),
    )
    return WebhookResult(outcome="accepted", event_id=event_id)
