"""Webhook ingress tests (plan 19, 48.5, 48.6): signature acceptance and
rejection, delivery-id dedupe, ping handling, and 404-not-leaking — with a
recording JetStream stub instead of NATS."""

import json
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service as connections_service
from jhin_api.deps import WorkspaceContext
from jhin_api.webhooks import service as webhooks
from jhin_connectors.github.webhook import sign_payload
from jhin_db.models import AuditEvent, Connection, WebhookDelivery
from jhin_domain import new_uuid7
from jhin_events.envelope import EventEnvelope
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "test"}

ISSUE_PAYLOAD = {
    "action": "opened",
    "issue": {"number": 7, "title": "Login broken", "state": "open", "user": {"login": "dev"}},
    "repository": {"full_name": "octo/alpha"},
    "sender": {"login": "dev"},
}


class RecordingJetStream:
    """Captures publishes; optionally fails to simulate a NATS outage."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        if self.fail:
            raise ConnectionError("nats is down")
        self.published.append((subject, payload, headers or {}))


@pytest.fixture
async def github_connection(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> tuple[Connection, str]:
    connection, webhook_secret = await connections_service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="github",
        name="GitHub main",
        auth_type="pat",
        credentials={"token": "fake-github-pat"},
        config={},
        **REQ,
    )
    assert webhook_secret is not None
    return connection, webhook_secret


def github_headers(secret: str, body: bytes, *, event: str, delivery: str) -> Mapping[str, str]:
    return {
        "X-Hub-Signature-256": sign_payload(secret, body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


async def deliver(
    session: AsyncSession,
    crypto: SecretCrypto,
    js: Any,
    connection: Connection,
    headers: Mapping[str, str],
    body: bytes,
) -> webhooks.WebhookResult:
    return await webhooks.process_delivery(
        session,
        crypto,
        js,
        connector_type="github",
        public_id=connection.public_id,
        headers=headers,
        body=body,
        **REQ,
    )


async def test_valid_signature_accepted_and_published(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    js = RecordingJetStream()
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-1")

    result = await deliver(session, crypto, js, connection, headers, body)

    assert result.outcome == "accepted"
    deliveries = (await session.scalars(select(WebhookDelivery))).all()
    assert [d.delivery_id for d in deliveries] == ["d-1"]

    (subject, payload, msg_headers) = js.published[0]
    assert subject == f"jhin.v1.{connection.workspace_id}.ingress.github.issues"
    envelope = EventEnvelope.from_bytes(payload)
    assert envelope.event_type == "ingress.github.issues"
    assert envelope.source.connection_id == connection.id
    assert envelope.data["payload"]["issue"]["number"] == 7
    assert msg_headers["Nats-Msg-Id"] == str(envelope.event_id)


async def test_invalid_signature_rejected_and_audited(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    js = RecordingJetStream()
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = dict(github_headers(secret, body, event="issues", delivery="d-2"))
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, js, connection, headers, body)

    assert excinfo.value.status_code == 401
    assert js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []
    audits = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "webhook.rejected"))
    ).all()
    assert len(audits) == 1
    assert audits[0].actor_type == "system"


async def test_tampered_body_rejected(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-3")
    tampered = body + b" "
    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(), connection, headers, tampered)
    assert excinfo.value.status_code == 401


async def test_duplicate_delivery_never_publishes_twice(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    js = RecordingJetStream()
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-same")

    first = await deliver(session, crypto, js, connection, headers, body)
    second = await deliver(session, crypto, js, connection, headers, body)

    assert first.outcome == "accepted"
    assert second.outcome == "duplicate"
    assert len(js.published) == 1
    assert len((await session.scalars(select(WebhookDelivery))).all()) == 1


async def test_ping_event_ignored_without_publish(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    js = RecordingJetStream()
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()
    headers = github_headers(secret, body, event="ping", delivery="d-ping")

    result = await deliver(session, crypto, js, connection, headers, body)

    assert result.outcome == "ignored"
    assert js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []


async def test_unknown_public_id_is_404(session: AsyncSession, crypto: SecretCrypto) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await webhooks.process_delivery(
            session,
            crypto,
            RecordingJetStream(),
            connector_type="github",
            public_id="0" * 32,
            headers={},
            body=b"{}",
            **REQ,
        )
    assert excinfo.value.status_code == 404


async def test_disabled_connection_is_404(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    github_connection: tuple[Connection, str],
) -> None:
    connection, secret = github_connection
    await connections_service.set_status(session, admin_ctx, connection.id, disabled=True, **REQ)
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-4")
    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(), connection, headers, body)
    assert excinfo.value.status_code == 404


async def test_nats_outage_rolls_back_delivery_row(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-5")

    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(fail=True), connection, headers, body)
    assert excinfo.value.status_code == 503
    # Row rolled back: the provider's retry will process cleanly.
    await session.refresh(connection)  # rollback expired the ORM row
    assert (await session.scalars(select(WebhookDelivery))).all() == []

    js = RecordingJetStream()
    result = await deliver(session, crypto, js, connection, headers, body)
    assert result.outcome == "accepted"
    assert len(js.published) == 1
