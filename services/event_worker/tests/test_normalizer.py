"""Ingress normalization tests (plan 9.4): raw GitHub ingress envelopes map
to canonical connector.* events with derived (deterministic) event ids."""

import json
from typing import Any

from jhin_connectors.vercel.connector import VercelConnector
from jhin_connectors.vercel.webhook import SIGNATURE_HEADER, sign_payload
from jhin_domain import new_uuid7
from jhin_event_worker.normalizer import IngressNormalizer, derived_event_id
from jhin_events.envelope import EventEnvelope, EventSource

VERCEL_WEBHOOK_SECRET = "exact-cap-vercel-webhook-secret"
VERCEL_WEBHOOK_BODY_CAP = 1_048_576


class FakeJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.published.append((subject, payload, headers or {}))


class DeduplicatingJetStream(FakeJetStream):
    """Tiny JetStream duplicate-window model keyed by Nats-Msg-Id."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_message_ids: set[str] = set()

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        normalized_headers = headers or {}
        message_id = normalized_headers.get("Nats-Msg-Id", "")
        if message_id and message_id in self.seen_message_ids:
            return
        if message_id:
            self.seen_message_ids.add(message_id)
        await super().publish(subject, payload, normalized_headers)


class FakeMsg:
    def __init__(self, data: bytes, subject: str = "jhin.v1.ws.ingress.github.issues") -> None:
        self.data = data
        self.subject = subject
        self.acked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True


def ingress_envelope(event: str, payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_type=f"ingress.github.{event}",
        workspace_id=str(new_uuid7()),
        source=EventSource(type="github", connection_id=new_uuid7()),
        data={"event": event, "delivery_id": "d-1", "payload": payload},
    )


def vercel_ingress_envelope(event: str = "deployment.ready") -> EventEnvelope:
    return EventEnvelope(
        event_type=f"ingress.vercel.{event}",
        workspace_id=str(new_uuid7()),
        source=EventSource(type="vercel", connection_id=new_uuid7()),
        data={
            "event": event,
            "delivery_id": "evt_123",
            "payload": {
                "id": "evt_123",
                "type": event,
                "createdAt": 1_700_000_000_000,
                "payload": {
                    "deployment": {
                        "id": "dpl_123",
                        "url": "storefront-abc.vercel.app",
                        "name": "storefront",
                        "meta": {
                            "githubCommitRef": "agent/fix",
                            "githubCommitSha": "abc123",
                            "token": "must-not-survive",
                        },
                    },
                    "project": {"id": "prj_123"},
                    "target": "preview",
                    "environment": {"DATABASE_URL": "must-not-survive"},
                },
            },
        },
    )


def exact_cap_vercel_webhook_body() -> bytes:
    payload: dict[str, Any] = {
        "id": "evt_exact_cap",
        "type": "deployment.ready",
        "createdAt": 1_700_000_000_000,
        "payload": {
            "deployment": {
                "id": "dpl_preview",
                "url": "dpl-preview.fake.vercel.app",
                "name": "github-project",
                "meta": {
                    "githubCommitRef": "main",
                    "githubCommitSha": "abc123",
                    "token": "must-not-survive",
                },
            },
            "project": {"id": "prj_github"},
            "target": "preview",
            "environment": {"DATABASE_URL": "must-not-survive"},
            "padding": "",
        },
    }
    compact = json.dumps(payload, separators=(",", ":")).encode()
    padding = VERCEL_WEBHOOK_BODY_CAP - len(compact)
    assert padding > 0
    provider_payload = payload["payload"]
    assert isinstance(provider_payload, dict)
    provider_payload["padding"] = "x" * padding
    exact = json.dumps(payload, separators=(",", ":")).encode()
    assert len(exact) == VERCEL_WEBHOOK_BODY_CAP
    return exact


async def test_issue_ingress_produces_canonical_event() -> None:
    js = FakeJetStream()
    envelope = ingress_envelope(
        "issues",
        {
            "action": "opened",
            "issue": {"number": 7, "title": "Bug", "state": "open", "user": {"login": "dev"}},
            "repository": {"full_name": "octo/alpha"},
            "sender": {"login": "dev"},
        },
    )
    msg = FakeMsg(envelope.to_bytes())

    await IngressNormalizer(js).handle(msg)  # type: ignore[arg-type]

    assert msg.acked and not msg.termed
    assert len(js.published) == 1
    subject, payload, headers = js.published[0]
    assert subject == f"jhin.v1.{envelope.workspace_id}.connector.github.issue.opened"
    out = EventEnvelope.from_bytes(payload)
    assert out.event_type == "connector.github.issue.opened"
    assert out.data["repository"] == "octo/alpha"
    assert out.causation_id == envelope.event_id
    # Derived id: identical on redelivery, so JetStream dedupes republished copies.
    assert out.event_id == derived_event_id(envelope.event_id, 0)
    assert headers["Nats-Msg-Id"] == str(out.event_id)


async def test_redelivery_produces_identical_event_ids() -> None:
    js = FakeJetStream()
    envelope = ingress_envelope(
        "push",
        {"ref": "refs/heads/main", "before": "a", "after": "b", "commits": []},
    )
    normalizer = IngressNormalizer(js)  # type: ignore[arg-type]
    await normalizer.handle(FakeMsg(envelope.to_bytes()))  # type: ignore[arg-type]
    await normalizer.handle(FakeMsg(envelope.to_bytes()))  # type: ignore[arg-type]

    ids = [EventEnvelope.from_bytes(p).event_id for _, p, _ in js.published]
    assert len(ids) == 2
    assert ids[0] == ids[1]  # JetStream duplicate window drops the second


async def test_vercel_dotted_success_ingress_publishes_one_safe_canonical_event() -> None:
    js = DeduplicatingJetStream()
    envelope = vercel_ingress_envelope()
    normalizer = IngressNormalizer(js)  # type: ignore[arg-type]

    # A crash can redeliver the ingress envelope. Both attempts derive the
    # same canonical UUID, so JetStream retains only one canonical message.
    first = FakeMsg(
        envelope.to_bytes(),
        subject=f"jhin.v1.{envelope.workspace_id}.ingress.vercel.deployment.ready",
    )
    second = FakeMsg(envelope.to_bytes(), subject=first.subject)
    await normalizer.handle(first)  # type: ignore[arg-type]
    await normalizer.handle(second)  # type: ignore[arg-type]

    assert first.acked and second.acked
    assert len(js.published) == 1
    subject, payload, headers = js.published[0]
    assert subject == f"jhin.v1.{envelope.workspace_id}.connector.vercel.deployment.ready"
    canonical = EventEnvelope.from_bytes(payload)
    assert canonical.event_id == derived_event_id(envelope.event_id, 0)
    assert headers["Nats-Msg-Id"] == str(canonical.event_id)
    assert canonical.data == {
        "deployment_id": "dpl_123",
        "project_id": "prj_123",
        "project_name": "storefront",
        "url": "storefront-abc.vercel.app",
        "target": "preview",
        "state": "READY",
        "created_at": 1_700_000_000_000,
        "git_ref": "agent/fix",
        "git_sha": "abc123",
    }
    assert "must-not-survive" not in payload.decode()


async def test_exact_cap_vercel_ingress_publishes_one_bounded_event_across_redelivery() -> None:
    body = exact_cap_vercel_webhook_body()
    connector = VercelConnector()
    raw = connector.parse_webhook(
        {SIGNATURE_HEADER: sign_payload(VERCEL_WEBHOOK_SECRET, body)},
        body,
        VERCEL_WEBHOOK_SECRET,
    )
    envelope = EventEnvelope(
        event_type="ingress.vercel.deployment.ready",
        workspace_id=str(new_uuid7()),
        source=EventSource(type="vercel", connection_id=new_uuid7()),
        data={
            "event": raw.event,
            "delivery_id": raw.delivery_id,
            "payload": raw.payload,
        },
    )
    js = DeduplicatingJetStream()
    normalizer = IngressNormalizer(js)  # type: ignore[arg-type]
    subject = f"jhin.v1.{envelope.workspace_id}.ingress.vercel.deployment.ready"
    first = FakeMsg(envelope.to_bytes(), subject=subject)
    retry = FakeMsg(envelope.to_bytes(), subject=subject)

    await normalizer.handle(first)  # type: ignore[arg-type]
    await normalizer.handle(retry)  # type: ignore[arg-type]

    assert first.acked and retry.acked
    assert len(js.published) == 1
    canonical_subject, canonical_bytes, headers = js.published[0]
    assert canonical_subject == (
        f"jhin.v1.{envelope.workspace_id}.connector.vercel.deployment.ready"
    )
    canonical = EventEnvelope.from_bytes(canonical_bytes)
    assert canonical.event_id == derived_event_id(envelope.event_id, 0)
    assert headers["Nats-Msg-Id"] == str(canonical.event_id)
    assert canonical.data == {
        "deployment_id": "dpl_preview",
        "project_id": "prj_github",
        "project_name": "github-project",
        "url": "dpl-preview.fake.vercel.app",
        "target": "preview",
        "state": "READY",
        "created_at": 1_700_000_000_000,
        "git_ref": "main",
        "git_sha": "abc123",
    }
    assert len(canonical_bytes) < 2_000
    assert "padding" not in canonical_bytes.decode()
    assert "must-not-survive" not in canonical_bytes.decode()


async def test_unknown_connector_or_event_is_terminated() -> None:
    js = FakeJetStream()
    envelope = EventEnvelope(
        event_type="ingress.mystery.thing",
        workspace_id=str(new_uuid7()),
        source=EventSource(type="mystery"),
        data={"event": "thing", "delivery_id": "d", "payload": {}},
    )
    msg = FakeMsg(envelope.to_bytes())
    await IngressNormalizer(js).handle(msg)  # type: ignore[arg-type]
    assert msg.termed and not msg.acked
    assert js.published == []


async def test_invalid_envelope_goes_to_dlq() -> None:
    js = FakeJetStream()
    msg = FakeMsg(json.dumps({"not": "an envelope"}).encode())
    await IngressNormalizer(js).handle(msg)  # type: ignore[arg-type]
    assert msg.termed
    assert len(js.published) == 1
    assert js.published[0][0] == "jhin.dlq.ingress"


async def test_unsupported_github_event_normalizes_to_nothing_but_acks() -> None:
    js = FakeJetStream()
    envelope = ingress_envelope("issues", {"action": "weird action!", "issue": {}})
    msg = FakeMsg(envelope.to_bytes())
    await IngressNormalizer(js).handle(msg)  # type: ignore[arg-type]
    assert msg.acked
    assert js.published == []
