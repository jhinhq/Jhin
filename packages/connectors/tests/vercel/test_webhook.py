"""Signed Vercel deployment webhook parsing and safe normalization."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from jhin_connectors.base import RawWebhookEvent, WebhookVerificationError
from jhin_connectors.testing.fake_vercel import FakeVercelServer
from jhin_connectors.vercel.connector import VercelConnector

SECRET = "vercel-webhook-secret-123"
SIGNATURE_HEADER = "x-vercel-signature"

PROVIDER_EVENTS = {
    "deployment.created",
    "deployment.ready",
    "deployment.succeeded",
    "deployment.error",
    "deployment.canceled",
    "deployment.promoted",
}

CANONICAL_EVENTS = {
    "connector.vercel.deployment.created",
    "connector.vercel.deployment.ready",
    "connector.vercel.deployment.error",
    "connector.vercel.deployment.canceled",
    "connector.vercel.deployment.promoted",
}

EXPECTED_READY_DATA = {
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


class _WebhookSink:
    def __init__(self) -> None:
        self.deliveries: list[tuple[dict[str, str], bytes]] = []
        self._lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with sink._lock:
                    sink.deliveries.append((dict(self.headers.items()), body))
                response = b'{"status":"accepted"}'
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/webhook"

    def __enter__(self) -> _WebhookSink:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_webhook_stack() -> Iterator[tuple[FakeVercelServer, _WebhookSink]]:
    with FakeVercelServer() as fake, _WebhookSink() as sink:
        yield fake, sink


def _payload(event: str = "deployment.ready", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "evt_123",
        "type": event,
        "createdAt": 1_700_000_000_000,
        "region": "sfo1",
        "payload": {
            "team": {"id": "team_private"},
            "user": {"id": "usr_private", "email": "private@example.com"},
            "deployment": {
                "id": "dpl_123",
                "url": "storefront-abc.vercel.app",
                "name": "storefront",
                "state": "PROVIDER_VALUE_MUST_NOT_OVERRIDE_EVENT_STATE",
                "meta": {
                    "githubCommitRef": "agent/fix",
                    "githubCommitSha": "abc123",
                    "token": "provider-token-must-not-survive",
                    "VERCEL_ENV": "secret-environment-value",
                },
                "arbitrary": "must-not-survive",
            },
            "project": {"id": "prj_123", "name": "ignored-project-copy"},
            "target": "preview",
            "links": {
                "deployment": "https://vercel.com/private/deployment",
                "project": "https://vercel.com/private/project",
            },
            "environment": {"DATABASE_URL": "postgres://must-not-survive"},
            "token": "payload-token-must-not-survive",
        },
        "top_level_private": "must-not-survive",
    }
    payload.update(overrides)
    return payload


def _body(event: str = "deployment.ready", **overrides: Any) -> bytes:
    return json.dumps(_payload(event, **overrides), separators=(",", ":")).encode()


def _signature(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def _raw(event: str, payload: dict[str, Any] | None = None) -> RawWebhookEvent:
    return RawWebhookEvent(
        event=event,
        delivery_id="evt_123",
        payload=payload if payload is not None else _payload(event),
    )


def test_manifest_declares_provider_supplied_sha1_and_stable_event_concepts() -> None:
    manifest = VercelConnector.manifest

    assert manifest.webhook_secret_mode == "provider_supplied"
    assert manifest.webhook_signature_algorithm == "hmac-sha1"
    assert set(manifest.webhook_events) == PROVIDER_EVENTS
    assert set(manifest.canonical_events) == CANONICAL_EVENTS
    assert manifest.supports_webhooks is True
    assert "Vercel" in manifest.webhook_setup_help


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "0" * 39,
        "0" * 41,
        "sha1=" + "0" * 40,
        "G" * 40,
        "A" * 40,
    ],
)
def test_missing_or_malformed_signature_is_rejected(signature: str | None) -> None:
    body = _body()
    headers = {} if signature is None else {SIGNATURE_HEADER: signature}

    with pytest.raises(WebhookVerificationError, match="x-vercel-signature"):
        VercelConnector().parse_webhook(headers, body, SECRET)


def test_wrong_signature_and_tampered_raw_bytes_are_rejected() -> None:
    body = _body()
    connector = VercelConnector()

    with pytest.raises(WebhookVerificationError):
        connector.parse_webhook(
            {SIGNATURE_HEADER: _signature(body, "different-secret")}, body, SECRET
        )
    with pytest.raises(WebhookVerificationError):
        connector.parse_webhook({SIGNATURE_HEADER: _signature(body)}, body + b" ", SECRET)


def test_signature_is_checked_before_json_parsing() -> None:
    with pytest.raises(WebhookVerificationError, match="x-vercel-signature"):
        VercelConnector().parse_webhook(
            {SIGNATURE_HEADER: "0" * 40}, b"not-json-and-must-not-be-parsed", SECRET
        )


def test_correct_signature_uses_root_delivery_id_and_event_type() -> None:
    body = _body()

    raw = VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)

    assert raw.delivery_id == "evt_123"
    assert raw.event == "deployment.ready"
    assert raw.payload == _payload()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"id": "", "type": "deployment.ready", "payload": {}},
        {"id": "x" * 201, "type": "deployment.ready", "payload": {}},
        {"id": "evt_123", "type": "", "payload": {}},
        {"id": "evt_123", "type": "x" * 101, "payload": {}},
        {"id": 123, "type": "deployment.ready", "payload": {}},
        {"id": "evt_123", "type": 123, "payload": {}},
    ],
)
def test_signed_malformed_root_or_oversized_ids_are_rejected(payload: Any) -> None:
    body = json.dumps(payload).encode()

    with pytest.raises(WebhookVerificationError):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


@pytest.mark.parametrize(
    "body",
    [
        b'{"id":"evt_123","type":"deployment.ready","payload":{"value":1e999}}',
        b'{"id":"evt_first","id":"evt_second","type":"deployment.ready","payload":{}}',
    ],
)
def test_signed_nonfinite_or_duplicate_key_json_is_rejected(body: bytes) -> None:
    with pytest.raises(WebhookVerificationError, match="valid JSON"):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


def test_signed_excessively_nested_json_is_rejected_without_recursion_escape() -> None:
    body = (
        b'{"id":"evt_123","type":"deployment.ready","payload":'
        + b"[" * 10_000
        + b"0"
        + b"]" * 10_000
        + b"}"
    )

    with pytest.raises(WebhookVerificationError, match="valid JSON"):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


def _body_with_provider_only_array_depth(array_depth: int) -> bytes:
    return (
        b'{"id":"evt_123","type":"deployment.ready","createdAt":1700000000000,'
        b'"payload":{"deployment":{"id":"dpl_123","url":"storefront.vercel.app",'
        b'"name":"storefront","providerOnly":'
        + b"[" * array_depth
        + b"0"
        + b"]" * array_depth
        + b'},"project":{"id":"prj_123"},"target":"preview"}}'
    )


def test_signed_json_nesting_accepts_exact_container_depth_boundary() -> None:
    # Root, provider payload, and deployment occupy three container levels;
    # 61 provider-only arrays bring the total to the fixed limit of 64.
    body = _body_with_provider_only_array_depth(61)

    raw = VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)

    assert raw.delivery_id == "evt_123"


def test_signed_json_nesting_over_limit_is_rejected_before_raw_event() -> None:
    body = _body_with_provider_only_array_depth(62)
    assert len(body) < 1_048_576

    with pytest.raises(WebhookVerificationError, match="nesting"):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


def _body_with_provider_only_fragment(fragment: bytes) -> bytes:
    return (
        b'{"id":"evt_123","type":"deployment.ready","createdAt":1700000000000,'
        b'"payload":{"deployment":{"id":"dpl_123","url":"storefront.vercel.app",'
        b'"name":"storefront"},"project":{"id":"prj_123"},"target":"preview",' + fragment + b"}}"
    )


@pytest.mark.parametrize(
    "fragment",
    [
        b'"providerOnly":"\\ud800"',
        b'"\\udfff":"providerOnly"',
    ],
    ids=["provider-only-value", "provider-only-key"],
)
def test_signed_json_rejects_lone_surrogates_anywhere_in_tree(fragment: bytes) -> None:
    body = _body_with_provider_only_fragment(fragment)

    with pytest.raises(WebhookVerificationError, match="Unicode"):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


def test_signed_json_accepts_valid_escaped_surrogate_pair_emoji() -> None:
    body = _body_with_provider_only_fragment(b'"providerOnly":"\\ud83d\\ude00"')

    raw = VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)

    assert raw.payload["payload"]["providerOnly"] == "😀"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "evt_123\n"},
        {"id": "evt_123\x7f"},
        {"type": "deployment.ready\t"},
        {"type": "deployment.ready\x00"},
    ],
)
def test_signed_root_identity_rejects_ascii_controls(overrides: dict[str, str]) -> None:
    body = json.dumps(_payload(**overrides)).encode()

    with pytest.raises(WebhookVerificationError):
        VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)


def test_root_delivery_id_and_event_type_accept_exact_storage_boundaries() -> None:
    payload = _payload(id="d" * 200, type="e" * 100)
    body = json.dumps(payload).encode()

    raw = VercelConnector().parse_webhook({SIGNATURE_HEADER: _signature(body)}, body, SECRET)

    assert raw.delivery_id == "d" * 200
    assert raw.event == "e" * 100


@pytest.mark.parametrize(
    ("provider_event", "canonical_event", "state"),
    [
        ("deployment.created", "connector.vercel.deployment.created", "BUILDING"),
        ("deployment.ready", "connector.vercel.deployment.ready", "READY"),
        ("deployment.succeeded", "connector.vercel.deployment.ready", "READY"),
        ("deployment.error", "connector.vercel.deployment.error", "ERROR"),
        ("deployment.canceled", "connector.vercel.deployment.canceled", "CANCELED"),
        ("deployment.promoted", "connector.vercel.deployment.promoted", "READY"),
    ],
)
def test_supported_provider_events_emit_one_fixed_shape(
    provider_event: str, canonical_event: str, state: str
) -> None:
    normalized = VercelConnector().normalize_event(_raw(provider_event))

    assert len(normalized) == 1
    assert normalized[0].event_type == canonical_event
    assert normalized[0].data == {**EXPECTED_READY_DATA, "state": state}


def test_ready_and_succeeded_share_one_canonical_concept_without_provider_fields() -> None:
    ready = VercelConnector().normalize_event(_raw("deployment.ready"))[0]
    succeeded = VercelConnector().normalize_event(_raw("deployment.succeeded"))[0]

    assert ready.event_type == succeeded.event_type == "connector.vercel.deployment.ready"
    assert ready.data == succeeded.data == EXPECTED_READY_DATA
    serialized = json.dumps(ready.data)
    for marker in (
        "provider-token-must-not-survive",
        "secret-environment-value",
        "private@example.com",
        "https://vercel.com/private/deployment",
        "must-not-survive",
    ):
        assert marker not in serialized


def test_git_metadata_uses_only_known_provider_keys_and_is_bounded() -> None:
    payload = _payload()
    provider_payload = payload["payload"]
    assert isinstance(provider_payload, dict)
    deployment = provider_payload["deployment"]
    assert isinstance(deployment, dict)
    deployment["meta"] = {
        "gitlabCommitRef": "release/one",
        "gitlabCommitSha": "def456",
        "randomRef": "not-allowed",
        "randomSha": "not-allowed",
    }

    event = VercelConnector().normalize_event(_raw("deployment.ready", payload))[0]

    assert event.data["git_ref"] == "release/one"
    assert event.data["git_sha"] == "def456"
    assert "not-allowed" not in json.dumps(event.data)


@pytest.mark.parametrize(
    ("provider", "ref_key", "sha_key"),
    [
        ("github", "githubCommitRef", "githubCommitSha"),
        ("gitlab", "gitlabCommitRef", "gitlabCommitSha"),
        ("bitbucket", "bitbucketCommitRef", "bitbucketCommitSha"),
    ],
)
def test_only_known_git_provider_meta_keys_are_supported(
    provider: str, ref_key: str, sha_key: str
) -> None:
    payload = _payload()
    provider_payload = payload["payload"]
    assert isinstance(provider_payload, dict)
    deployment = provider_payload["deployment"]
    assert isinstance(deployment, dict)
    deployment["meta"] = {ref_key: f"{provider}/ref", sha_key: "a1b2c3"}

    event = VercelConnector().normalize_event(_raw("deployment.ready", payload))[0]

    assert event.data["git_ref"] == f"{provider}/ref"
    assert event.data["git_sha"] == "a1b2c3"


def test_official_account_project_id_shape_is_supported() -> None:
    payload = _payload()
    provider_payload = payload["payload"]
    assert isinstance(provider_payload, dict)
    provider_payload.pop("project")
    provider_payload["projectId"] = "prj_account_shape"

    event = VercelConnector().normalize_event(_raw("deployment.ready", payload))[0]

    assert event.data["project_id"] == "prj_account_shape"


def _valid_hostname_at_limit() -> str:
    # Four DNS labels, each within 63 chars, totaling the 253-character DNS limit.
    return ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))


@pytest.mark.parametrize(
    ("output_field", "container", "provider_key", "at_limit", "over_limit"),
    [
        ("deployment_id", "deployment", "id", "d" * 200, "d" * 201),
        ("project_id", "project", "id", "p" * 200, "p" * 201),
        ("project_name", "deployment", "name", "n" * 200, "n" * 201),
        ("url", "deployment", "url", _valid_hostname_at_limit(), _valid_hostname_at_limit() + "x"),
        ("target", "payload", "target", "production", "productionx"),
        ("git_ref", "meta", "githubCommitRef", "r" * 512, "r" * 513),
        ("git_sha", "meta", "githubCommitSha", "a" * 128, "a" * 129),
    ],
)
def test_every_emitted_provider_string_accepts_its_boundary_and_rejects_overflow(
    output_field: str,
    container: str,
    provider_key: str,
    at_limit: str,
    over_limit: str,
) -> None:
    def set_value(payload: dict[str, Any], value: str) -> None:
        provider_payload = payload["payload"]
        assert isinstance(provider_payload, dict)
        deployment = provider_payload["deployment"]
        assert isinstance(deployment, dict)
        project = provider_payload["project"]
        assert isinstance(project, dict)
        meta = deployment["meta"]
        assert isinstance(meta, dict)
        targets = {
            "payload": provider_payload,
            "deployment": deployment,
            "project": project,
            "meta": meta,
        }
        targets[container][provider_key] = value

    boundary_payload = _payload()
    set_value(boundary_payload, at_limit)
    boundary = VercelConnector().normalize_event(_raw("deployment.ready", boundary_payload))
    assert len(boundary) == 1
    assert boundary[0].data[output_field] == at_limit

    overflow_payload = _payload()
    set_value(overflow_payload, over_limit)
    assert VercelConnector().normalize_event(_raw("deployment.ready", overflow_payload)) == []


def test_provider_state_is_never_copied_even_when_hostile_or_oversized() -> None:
    payload = _payload()
    provider_payload = payload["payload"]
    assert isinstance(provider_payload, dict)
    deployment = provider_payload["deployment"]
    assert isinstance(deployment, dict)
    deployment["state"] = "private-state-" + "x" * 10_000

    event = VercelConnector().normalize_event(_raw("deployment.ready", payload))[0]

    assert event.data["state"] == "READY"
    assert "private-state" not in json.dumps(event.data)


def test_unknown_or_malformed_signed_events_normalize_to_nothing() -> None:
    assert VercelConnector().normalize_event(_raw("project.created")) == []
    assert VercelConnector().normalize_event(_raw("deployment.ready", {})) == []

    missing_deployment = _payload()
    provider_payload = missing_deployment["payload"]
    assert isinstance(provider_payload, dict)
    provider_payload.pop("deployment")
    assert VercelConnector().normalize_event(_raw("deployment.ready", missing_deployment)) == []

    oversized = _payload()
    provider_payload = oversized["payload"]
    assert isinstance(provider_payload, dict)
    deployment = provider_payload["deployment"]
    assert isinstance(deployment, dict)
    deployment["name"] = "x" * 301
    assert VercelConnector().normalize_event(_raw("deployment.ready", oversized)) == []


def test_fake_admin_emitter_signs_exact_bytes_without_retaining_secret(
    fake_webhook_stack: tuple[FakeVercelServer, _WebhookSink],
) -> None:
    fake, sink = fake_webhook_stack

    response = httpx.post(
        f"{fake.base_url}/_admin/webhook",
        json={
            "url": sink.url,
            "secret": SECRET,
            "event": "deployment.ready",
            "deployment_id": "dpl_preview",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "delivered": True,
        "delivery_id": "evt_webhook_1",
        "response": 202,
    }
    assert len(sink.deliveries) == 1
    headers, body = sink.deliveries[0]
    raw = VercelConnector().parse_webhook(headers, body, SECRET)
    assert raw.delivery_id == "evt_webhook_1"
    assert raw.event == "deployment.ready"
    assert raw.payload["payload"]["deployment"]["id"] == "dpl_preview"
    received_signature = next(
        value for name, value in headers.items() if name.lower() == "x-vercel-signature"
    )
    assert received_signature == _signature(body)

    retained = response.text + json.dumps(fake.state.snapshot())
    assert SECRET not in retained
