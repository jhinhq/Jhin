"""Webhook ingress tests (plan 19, 48.5, 48.6): signature acceptance and
rejection, delivery-id dedupe, ping handling, and 404-not-leaking — with a
recording JetStream stub instead of NATS."""

import json
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
import structlog
from fastapi import FastAPI, HTTPException, Request
from opentelemetry import baggage, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags, Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_api.connections import service as connections_service
from jhin_api.deps import WorkspaceContext, get_db, get_jetstream
from jhin_api.main import HttpObservabilityMiddleware
from jhin_api.webhooks import router as webhook_router_module
from jhin_api.webhooks import service as webhooks
from jhin_api.webhooks.router import router as webhooks_router
from jhin_connectors import WebhookVerificationError, default_registry
from jhin_connectors.github.manifest import GITHUB_MANIFEST
from jhin_connectors.github.webhook import sign_payload
from jhin_connectors.vercel.webhook import sign_payload as sign_vercel_payload
from jhin_db.models import (
    Agent,
    AuditEvent,
    Connection,
    Trigger,
    TriggerInvocation,
    WebhookDelivery,
)
from jhin_domain import AgentStatus, new_uuid7
from jhin_event_worker.matcher import TriggerMatcher
from jhin_event_worker.normalizer import IngressNormalizer, derived_event_id
from jhin_event_worker.processor import EventProcessor
from jhin_events.envelope import EventEnvelope
from jhin_events.telemetry import dispatch_or_nak
from jhin_observability import noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "test"}
MAX_WEBHOOK_BODY_BYTES = 1_048_576

ISSUE_PAYLOAD = {
    "action": "opened",
    "issue": {"number": 7, "title": "Login broken", "state": "open", "user": {"login": "dev"}},
    "repository": {"full_name": "octo/alpha"},
    "sender": {"login": "dev"},
}

REMOTE_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
REMOTE_TRACESTATE = "vendor=value"


class RecordingJetStream:
    """Captures publishes; optionally fails to simulate a NATS outage."""

    def __init__(
        self,
        *,
        fail: bool = False,
        timeline: list[str] | None = None,
        publish_event: str = "publish",
    ) -> None:
        self.fail = fail
        self.timeline = timeline
        self.publish_event = publish_event
        self.published: list[tuple[str, bytes, dict[str, str]]] = []
        self.publish_span_ids: list[int] = []

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self.publish_span_ids.append(trace.get_current_span().get_span_context().span_id)
        if self.timeline is not None:
            self.timeline.append(self.publish_event)
        if self.fail:
            raise ConnectionError("nats is down")
        self.published.append((subject, payload, headers or {}))


class DeduplicatingRecordingJetStream(RecordingJetStream):
    """Records the first message for each JetStream id, like its dedupe window."""

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


class IngressMessage:
    def __init__(self, subject: str, data: bytes) -> None:
        self.subject = subject
        self.data = data
        self.acked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True


class TraceBridgeMessage:
    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: Mapping[str, str],
        *,
        timeline: list[str] | None = None,
        ack_event: str = "ack",
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = dict(headers)
        self.metadata = SimpleNamespace(num_delivered=1)
        self.acks = 0
        self.terms = 0
        self.naks = 0
        self.timeline = timeline
        self.ack_event = ack_event
        self.ack_span_ids: list[int] = []

    async def ack(self) -> None:
        self.acks += 1
        self.ack_span_ids.append(trace.get_current_span().get_span_context().span_id)
        if self.timeline is not None:
            self.timeline.append(self.ack_event)

    async def term(self) -> None:
        self.terms += 1

    async def nak(self, *, delay: int) -> None:
        assert delay == 2
        self.naks += 1


@dataclass
class TraceService:
    provider: TracerProvider
    tracer: Tracer
    exporter: InMemorySpanExporter


@contextmanager
def trace_service(service_name: str) -> Iterator[TraceService]:
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": service_name,
                "service.version": "test",
                "deployment.environment.name": "test",
            }
        )
    )
    try:
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        yield TraceService(
            provider,
            provider.get_tracer(f"{service_name}-test"),
            exporter,
        )
    finally:
        provider.shutdown()


def track_provider_shutdowns(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    shutdown_ids: list[int] = []
    original_shutdown = TracerProvider.shutdown

    def tracked_shutdown(provider: TracerProvider) -> None:
        shutdown_ids.append(id(provider))
        original_shutdown(provider)

    monkeypatch.setattr(TracerProvider, "shutdown", tracked_shutdown)
    return shutdown_ids


def test_trace_services_close_every_provider_when_second_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_ids = track_provider_shutdowns(monkeypatch)
    real_exporter = InMemorySpanExporter
    exporter_calls = 0
    setup_error = RuntimeError("second-provider-setup-canary")

    def exporter_factory() -> InMemorySpanExporter:
        nonlocal exporter_calls
        exporter_calls += 1
        if exporter_calls == 2:
            raise setup_error
        return real_exporter()

    monkeypatch.setattr(sys.modules[__name__], "InMemorySpanExporter", exporter_factory)

    with pytest.raises(RuntimeError) as excinfo, ExitStack() as stack:
        stack.enter_context(trace_service("api"))
        stack.enter_context(trace_service("event-worker"))
    assert excinfo.value is setup_error
    assert len(shutdown_ids) == 2
    assert len(set(shutdown_ids)) == 2


def test_trace_services_close_after_post_acquisition_fastapi_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_ids = track_provider_shutdowns(monkeypatch)
    setup_error = RuntimeError("fastapi-setup-canary")

    def fail_add_middleware(self: FastAPI, *_args: Any, **_kwargs: Any) -> None:
        del self
        raise setup_error

    monkeypatch.setattr(FastAPI, "add_middleware", fail_add_middleware)

    with pytest.raises(RuntimeError) as excinfo, ExitStack() as stack:
        stack.enter_context(trace_service("api"))
        stack.enter_context(trace_service("event-worker"))
        app = FastAPI()
        app.add_middleware(HttpObservabilityMiddleware)
    assert excinfo.value is setup_error
    assert len(shutdown_ids) == 2
    assert len(set(shutdown_ids)) == 2


def serialized_trace_span(span: ReadableSpan) -> str:
    return json.dumps(
        {
            "name": span.name,
            "context": {
                "trace_id": format(span.context.trace_id, "032x") if span.context else None,
                "span_id": format(span.context.span_id, "016x") if span.context else None,
            },
            "parent": {
                "trace_id": format(span.parent.trace_id, "032x") if span.parent else None,
                "span_id": format(span.parent.span_id, "016x") if span.parent else None,
            },
            "resource": dict(span.resource.attributes),
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
            "status": {
                "code": span.status.status_code.name,
                "description": span.status.description,
            },
        },
        default=str,
        sort_keys=True,
    )


def span_id_from_traceparent(value: str) -> int:
    return int(value.split("-")[2], 16)


def require_parent(span: ReadableSpan) -> SpanContext:
    parent = span.parent
    assert parent is not None
    return parent


def assert_telemetry_context_empty() -> None:
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()
    assert not baggage.get_all()


class RecordingTemporal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, name: str, params: Any, *, id: str, task_queue: str) -> None:
        if any(call["id"] == id for call in self.calls):
            # Temporal owns duplicate-start idempotency for a deterministic id.
            raise WorkflowAlreadyStartedError(id, name)
        self.calls.append({"name": name, "params": params, "id": id, "task_queue": task_queue})


class EchoingVerificationConnector:
    manifest = GITHUB_MANIFEST

    def parse_webhook(
        self,
        _headers: Mapping[str, str],
        body: bytes,
        secret: str,
    ) -> Any:
        raise WebhookVerificationError(f"echoed {secret} and {body.decode()}")


class SingleConnectorRegistry:
    def __init__(self, connector: EchoingVerificationConnector) -> None:
        self.connector = connector

    def get(self, connector_type: str) -> EchoingVerificationConnector | None:
        return self.connector if connector_type == "github" else None


class ChunkedRequestBody(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.chunks_yielded += 1
            yield chunk


class TrackingBytearray(bytearray):
    extended_sizes: ClassVar[list[int]] = []

    def extend(self, value: Any) -> None:
        type(self).extended_sizes.append(len(value))
        super().extend(value)


@dataclass
class WebhookRouteHarness:
    client: httpx.AsyncClient
    processed_bodies: list[bytes]
    processed_kwargs: list[dict[str, Any]]
    js: RecordingJetStream
    tracer: object


@pytest.fixture
async def webhook_routes(
    session: AsyncSession,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WebhookRouteHarness]:
    processed_bodies: list[bytes] = []
    processed_kwargs: list[dict[str, Any]] = []
    js = RecordingJetStream()
    tracer = object()

    async def fake_process_delivery(
        _db: AsyncSession,
        _crypto: SecretCrypto,
        _js: Any,
        **kwargs: Any,
    ) -> webhooks.WebhookResult:
        processed_bodies.append(kwargs["body"])
        processed_kwargs.append(kwargs)
        return webhooks.WebhookResult(outcome="accepted", event_id=new_uuid7())

    monkeypatch.setattr(webhooks, "process_delivery", fake_process_delivery)
    app = FastAPI()
    app.state.secret_crypto = crypto
    app.state.observability = SimpleNamespace(tracer=tracer)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(webhooks_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_jetstream() -> RecordingJetStream:
        return js

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_jetstream] = override_jetstream
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield WebhookRouteHarness(
            client=client,
            processed_bodies=processed_bodies,
            processed_kwargs=processed_kwargs,
            js=js,
            tracer=tracer,
        )


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


async def test_real_webhook_to_worker_path_has_exact_five_span_parent_graph(
    session: AsyncSession,
    crypto: SecretCrypto,
    github_connection: tuple[Connection, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection, secret = github_connection
    trace_stack = ExitStack()
    api_trace = trace_stack.enter_context(trace_service("api"))
    try:
        worker_trace = trace_stack.enter_context(trace_service("event-worker"))
        timeline: list[str] = []
        ingress_js = RecordingJetStream(
            timeline=timeline,
            publish_event="INGRESS publish",
        )
        events_js = RecordingJetStream(
            timeline=timeline,
            publish_event="EVENTS publish",
        )
        matched: list[EventEnvelope] = []
        matched_span_ids: list[int] = []

        class RecordingMatcher:
            async def handle_event(self, envelope: EventEnvelope) -> None:
                matched.append(envelope)
                matched_span_ids.append(trace.get_current_span().get_span_context().span_id)
                timeline.append("matcher")

        app = FastAPI()
        app.state.secret_crypto = crypto
        app.state.observability = SimpleNamespace(tracer=api_trace.tracer)
        app.add_middleware(HttpObservabilityMiddleware)
        app.include_router(webhooks_router)

        async def override_db() -> AsyncIterator[AsyncSession]:
            yield session

        async def override_jetstream() -> RecordingJetStream:
            return ingress_js

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_jetstream] = override_jetstream
        body = json.dumps(ISSUE_PAYLOAD, separators=(",", ":")).encode()
        request_headers = dict(
            github_headers(
                secret,
                body,
                event="issues",
                delivery="five-span-delivery",
            )
        )
        request_headers.update(
            {
                "traceparent": REMOTE_TRACEPARENT,
                "tracestate": REMOTE_TRACESTATE,
                "baggage": "private-baggage-canary=do-not-propagate",
                "authorization": "Bearer private-authorization-canary",
                "cookie": "session=private-cookie-canary",
                "x-private-canary": "private-header-value-canary",
            }
        )
        capsys.readouterr()

        with structlog.testing.capture_logs() as captured_logs:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    f"/api/v1/webhooks/github/{connection.public_id}",
                    content=body,
                    headers=request_headers,
                )
            assert_telemetry_context_empty()
            assert response.status_code == 202
            assert response.json()["status"] == "accepted"
            assert len(ingress_js.published) == 1
            delivery = await session.scalar(
                select(WebhookDelivery).where(WebhookDelivery.delivery_id == "five-span-delivery")
            )
            assert delivery is not None
            assert response.json()["event_id"] == str(delivery.event_id)

            ingress_subject, ingress_payload, ingress_headers = ingress_js.published[0]
            ingress_message = TraceBridgeMessage(
                ingress_subject,
                ingress_payload,
                ingress_headers,
                timeline=timeline,
                ack_event="INGRESS ack",
            )
            normalizer = IngressNormalizer(events_js, tracer=worker_trace.tracer)  # type: ignore[arg-type]
            await dispatch_or_nak(
                ingress_message,  # type: ignore[arg-type]
                tracer=worker_trace.tracer,
                stream="INGRESS",
                durable="event-worker-ingress",
                handler=normalizer.handle,  # type: ignore[arg-type]
            )
            assert_telemetry_context_empty()
            assert (ingress_message.acks, ingress_message.terms, ingress_message.naks) == (
                1,
                0,
                0,
            )
            assert len(events_js.published) == 1

            event_subject, event_payload, event_headers = events_js.published[0]
            event_message = TraceBridgeMessage(
                event_subject,
                event_payload,
                event_headers,
                timeline=timeline,
                ack_event="EVENTS ack",
            )
            processor = EventProcessor(
                events_js,  # type: ignore[arg-type]
                matcher=RecordingMatcher(),
                tracer=worker_trace.tracer,
            )
            await dispatch_or_nak(
                event_message,  # type: ignore[arg-type]
                tracer=worker_trace.tracer,
                stream="EVENTS",
                durable="event-worker",
                handler=processor.handle,  # type: ignore[arg-type]
            )
            assert_telemetry_context_empty()
            assert (event_message.acks, event_message.terms, event_message.naks) == (1, 0, 0)
            captured_streams = capsys.readouterr()

        api_spans = list(api_trace.exporter.get_finished_spans())
        worker_spans = list(worker_trace.exporter.get_finished_spans())
        assert len(api_spans) == 2
        assert len(worker_spans) == 3
        assert len(api_spans) + len(worker_spans) == 5
        assert {span.resource.attributes["service.name"] for span in api_spans} == {"api"}
        assert {span.resource.attributes["service.name"] for span in worker_spans} == {
            "event-worker"
        }

        server = next(span for span in api_spans if span.name == "http.server.request")
        ingress_producer = next(span for span in api_spans if span.name == "nats.publish")
        ingress_consumer = next(
            span
            for span in worker_spans
            if span.name == "nats.consume"
            and dict(span.attributes or {})["jhin.stream"] == "INGRESS"
        )
        events_producer = next(span for span in worker_spans if span.name == "nats.publish")
        events_consumer = next(
            span
            for span in worker_spans
            if span.name == "nats.consume"
            and dict(span.attributes or {})["jhin.stream"] == "EVENTS"
        )
        ingress_envelope = EventEnvelope.from_bytes(ingress_payload)
        event_envelope = EventEnvelope.from_bytes(event_payload)
        assert ingress_subject == (f"jhin.v1.{ingress_envelope.workspace_id}.ingress.github.issues")
        assert event_subject == (
            f"jhin.v1.{event_envelope.workspace_id}.connector.github.issue.opened"
        )
        for span in (
            server,
            ingress_producer,
            ingress_consumer,
            events_producer,
            events_consumer,
        ):
            assert span.context is not None
            assert span.context.trace_flags == TraceFlags.SAMPLED
            assert span.context.trace_state.to_header() == REMOTE_TRACESTATE
            assert span.status.status_code.name == "UNSET"
            assert span.status.description is None
        server_parent = require_parent(server)
        ingress_producer_parent = require_parent(ingress_producer)
        ingress_consumer_parent = require_parent(ingress_consumer)
        events_producer_parent = require_parent(events_producer)
        events_consumer_parent = require_parent(events_consumer)
        assert server.kind is SpanKind.SERVER
        assert ingress_producer.kind is SpanKind.PRODUCER
        assert ingress_consumer.kind is SpanKind.CONSUMER
        assert events_producer.kind is SpanKind.PRODUCER
        assert events_consumer.kind is SpanKind.CONSUMER
        assert dict(server.attributes or {}) == {
            "http.request.method": "POST",
            "http.response.status_class": "2xx",
            "http.response.status_code": 202,
            "http.route": "/api/:path*",
        }
        assert dict(ingress_producer.attributes or {}) == {
            "messaging.system": "nats",
            "jhin.stream": "INGRESS",
            "jhin.subject_family": "ingress",
            "jhin.outcome": "ok",
        }
        assert dict(ingress_consumer.attributes or {}) == {
            "messaging.system": "nats",
            "jhin.stream": "INGRESS",
            "jhin.consumer": "event-worker-ingress",
            "jhin.subject_family": "ingress",
            "jhin.outcome": "ok",
            "jhin.correlation_id": str(ingress_envelope.correlation_id),
            "jhin.workspace_id": ingress_envelope.workspace_id,
        }
        assert dict(events_producer.attributes or {}) == {
            "messaging.system": "nats",
            "jhin.stream": "EVENTS",
            "jhin.subject_family": "connector",
            "jhin.outcome": "ok",
        }
        assert dict(events_consumer.attributes or {}) == {
            "messaging.system": "nats",
            "jhin.stream": "EVENTS",
            "jhin.consumer": "event-worker",
            "jhin.subject_family": "connector",
            "jhin.outcome": "ok",
            "jhin.correlation_id": str(event_envelope.correlation_id),
            "jhin.workspace_id": event_envelope.workspace_id,
        }
        remote_trace_id = int(REMOTE_TRACEPARENT.split("-")[1], 16)
        assert server_parent.span_id == span_id_from_traceparent(REMOTE_TRACEPARENT)
        assert server_parent.trace_id == remote_trace_id
        assert server_parent.trace_flags == TraceFlags.SAMPLED
        assert server_parent.trace_state.to_header() == REMOTE_TRACESTATE
        assert server_parent.is_remote is True
        assert ingress_producer_parent.span_id == server.context.span_id
        assert ingress_producer_parent.trace_id == server.context.trace_id
        assert ingress_producer_parent.is_remote is False
        assert ingress_consumer_parent.span_id == ingress_producer.context.span_id
        assert ingress_consumer_parent.trace_id == ingress_producer.context.trace_id
        assert ingress_consumer_parent.trace_flags == ingress_producer.context.trace_flags
        assert ingress_consumer_parent.trace_state == ingress_producer.context.trace_state
        assert ingress_consumer_parent.is_remote is True
        assert events_producer_parent.span_id == ingress_consumer.context.span_id
        assert events_producer_parent.trace_id == ingress_consumer.context.trace_id
        assert events_producer_parent.is_remote is False
        assert events_consumer_parent.span_id == events_producer.context.span_id
        assert events_consumer_parent.trace_id == events_producer.context.trace_id
        assert events_consumer_parent.trace_flags == events_producer.context.trace_flags
        assert events_consumer_parent.trace_state == events_producer.context.trace_state
        assert events_consumer_parent.is_remote is True
        assert {span.context.trace_id for span in (*api_spans, *worker_spans)} == {remote_trace_id}

        assert matched == [event_envelope]
        assert timeline == [
            "INGRESS publish",
            "EVENTS publish",
            "INGRESS ack",
            "matcher",
            "EVENTS ack",
        ]
        assert ingress_envelope.event_id == delivery.event_id
        assert event_envelope.event_id == derived_event_id(ingress_envelope.event_id, 0)
        assert event_envelope.event_type == "connector.github.issue.opened"
        assert event_envelope.causation_id == ingress_envelope.event_id
        assert event_envelope.correlation_id == ingress_envelope.correlation_id
        assert event_envelope.workspace_id == ingress_envelope.workspace_id
        assert event_envelope.source == ingress_envelope.source
        assert set(ingress_headers) == {"Nats-Msg-Id", "traceparent", "tracestate"}
        assert set(event_headers) == {"Nats-Msg-Id", "traceparent", "tracestate"}
        assert ingress_headers["Nats-Msg-Id"] == str(ingress_envelope.event_id)
        assert event_headers["Nats-Msg-Id"] == str(event_envelope.event_id)
        assert ingress_headers["tracestate"] == REMOTE_TRACESTATE
        assert event_headers["tracestate"] == REMOTE_TRACESTATE
        assert (
            span_id_from_traceparent(ingress_headers["traceparent"])
            == ingress_producer.context.span_id
        )
        assert (
            span_id_from_traceparent(event_headers["traceparent"])
            == events_producer.context.span_id
        )
        assert ingress_js.publish_span_ids == [ingress_producer.context.span_id]
        assert events_js.publish_span_ids == [events_producer.context.span_id]
        assert ingress_message.ack_span_ids == [ingress_consumer.context.span_id]
        assert event_message.ack_span_ids == [events_consumer.context.span_id]
        assert matched_span_ids == [events_consumer.context.span_id]
        assert all(
            sum(key.lower() == carrier for key in headers) == 1
            for headers in (ingress_headers, event_headers)
            for carrier in ("traceparent", "tracestate")
        )
        assert all(
            "baggage" not in {key.lower() for key in headers}
            for headers in (ingress_headers, event_headers)
        )

        rendered_telemetry = "\n".join(
            [
                *(serialized_trace_span(span) for span in (*api_spans, *worker_spans)),
                json.dumps(captured_logs, default=str, sort_keys=True),
                captured_streams.out,
                captured_streams.err,
            ]
        )
        for canary in (
            "Login broken",
            "octo/alpha",
            "private-baggage-canary",
            "private-authorization-canary",
            "private-cookie-canary",
            "private-description-canary",
            "x-private-canary",
            "private-header-value-canary",
            secret,
            request_headers["X-Hub-Signature-256"],
            connection.public_id,
            "five-span-delivery",
        ):
            assert canary not in rendered_telemetry
    finally:
        trace_stack.close()


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


async def test_webhook_body_exactly_one_mib_is_accepted(
    webhook_routes: WebhookRouteHarness,
) -> None:
    body = b"x" * MAX_WEBHOOK_BODY_BYTES

    response = await webhook_routes.client.post(
        "/api/v1/webhooks/github/public-id",
        content=ChunkedRequestBody(body),
    )

    assert response.status_code == 202, response.text
    assert webhook_routes.processed_bodies == [body]


async def test_webhook_route_passes_the_lifespan_runtime_tracer(
    webhook_routes: WebhookRouteHarness,
) -> None:
    response = await webhook_routes.client.post(
        "/api/v1/webhooks/github/public-id",
        content=b"{}",
    )

    assert response.status_code == 202, response.text
    assert len(webhook_routes.processed_kwargs) == 1
    assert webhook_routes.processed_kwargs[0]["tracer"] is webhook_routes.tracer


async def test_webhook_body_one_byte_over_cap_is_rejected_before_parse_or_publish(
    webhook_routes: WebhookRouteHarness,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    TrackingBytearray.extended_sizes = []
    monkeypatch.setattr(webhook_router_module, "bytearray", TrackingBytearray, raising=False)
    body = ChunkedRequestBody(b"x" * MAX_WEBHOOK_BODY_BYTES, b"y")

    response = await webhook_routes.client.post(
        "/api/v1/webhooks/github/public-id",
        content=body,
    )

    assert response.status_code == 413, response.text
    assert TrackingBytearray.extended_sizes == [MAX_WEBHOOK_BODY_BYTES]
    assert webhook_routes.processed_bodies == []
    assert webhook_routes.js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []


async def test_one_huge_asgi_chunk_is_rejected_before_copy(
    webhook_routes: WebhookRouteHarness,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    TrackingBytearray.extended_sizes = []
    monkeypatch.setattr(webhook_router_module, "bytearray", TrackingBytearray, raising=False)
    body = ChunkedRequestBody(b"x" * (MAX_WEBHOOK_BODY_BYTES + 1))

    response = await webhook_routes.client.post(
        "/api/v1/webhooks/github/public-id",
        content=body,
    )

    assert response.status_code == 413, response.text
    assert TrackingBytearray.extended_sizes == []
    assert webhook_routes.processed_bodies == []
    assert webhook_routes.js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []


async def test_content_length_over_cap_rejects_before_stream_iteration(
    webhook_routes: WebhookRouteHarness,
    session: AsyncSession,
) -> None:
    body = ChunkedRequestBody(b"must-not-be-read")

    response = await webhook_routes.client.post(
        "/api/v1/webhooks/github/public-id",
        content=body,
        headers={"content-length": str(MAX_WEBHOOK_BODY_BYTES + 1)},
    )

    assert response.status_code == 413, response.text
    assert body.chunks_yielded == 0
    assert webhook_routes.processed_bodies == []
    assert webhook_routes.js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []


async def test_process_delivery_rejects_body_over_cap_before_connector_lookup(
    session: AsyncSession,
    crypto: SecretCrypto,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_registry_lookup() -> Any:
        pytest.fail("oversized body reached connector lookup")

    monkeypatch.setattr(webhooks, "default_registry", unexpected_registry_lookup)

    with pytest.raises(HTTPException) as excinfo:
        await webhooks.process_delivery(
            session,
            crypto,
            RecordingJetStream(),
            connector_type="github",
            public_id="0" * 32,
            headers={},
            body=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
            **REQ,
        )

    assert excinfo.value.status_code == 413


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


def test_ingress_event_id_is_stable_for_connector_connection_and_delivery() -> None:
    connection_id = UUID("11111111-2222-3333-4444-555555555555")

    first = webhooks.ingress_event_id("vercel", connection_id, "delivery-42")
    second = webhooks.ingress_event_id("vercel", connection_id, "delivery-42")

    assert first == UUID("c3a160bb-6079-52b8-9455-32abdb462d15")
    assert second == first


async def test_retry_after_publish_before_commit_reuses_event_id(
    session: AsyncSession,
    crypto: SecretCrypto,
    github_connection: tuple[Connection, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, secret = github_connection
    public_id = connection.public_id
    js = RecordingJetStream()
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = github_headers(secret, body, event="issues", delivery="d-commit-crash")
    original_commit = session.commit

    async def fail_before_commit() -> None:
        raise RuntimeError("injected pre-commit failure")

    monkeypatch.setattr(session, "commit", fail_before_commit)
    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, js, connection, headers, body)
    assert excinfo.value.status_code == 503
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    monkeypatch.setattr(session, "commit", original_commit)

    bind = session.bind
    assert bind is not None
    fresh_sessions = async_sessionmaker(bind, expire_on_commit=False)
    async with fresh_sessions() as fresh_session:
        retry = await webhooks.process_delivery(
            fresh_session,
            crypto,
            js,
            connector_type="github",
            public_id=public_id,
            headers=headers,
            body=body,
            **REQ,
        )
        deliveries = (await fresh_session.scalars(select(WebhookDelivery))).all()

    assert retry.outcome == "accepted"
    assert len(deliveries) == 1
    assert len(js.published) == 2
    first_subject, first_payload, first_headers = js.published[0]
    second_subject, second_payload, second_headers = js.published[1]
    first_event = EventEnvelope.from_bytes(first_payload)
    second_event = EventEnvelope.from_bytes(second_payload)
    assert second_subject == first_subject
    assert second_event.event_id == first_event.event_id
    assert first_headers["Nats-Msg-Id"] == second_headers["Nats-Msg-Id"]
    assert first_headers["Nats-Msg-Id"] == str(first_event.event_id)
    assert deliveries[0].event_id == first_event.event_id


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
    # The one case that stays vague on purpose: it says the signature did not
    # match and what to do, and nothing about the secret itself.
    assert "Signature verification failed" in str(excinfo.value.detail)
    assert "signing secret" in str(excinfo.value.detail)
    assert js.published == []
    assert (await session.scalars(select(WebhookDelivery))).all() == []
    audits = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "webhook.rejected"))
    ).all()
    assert len(audits) == 1
    assert audits[0].actor_type == "system"
    assert audits[0].metadata_json["reason"] == "verification_failed"


async def test_a_signed_but_unparseable_body_is_not_reported_as_a_bad_signature(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    """Verification covers more than the HMAC, and every failure used to come
    back as "Signature verification failed" — sending people to rotate a
    secret that was never wrong."""
    connection, secret = github_connection
    body = b"<html>not json at all</html>"
    headers = github_headers(secret, body, event="issues", delivery="d-broken")

    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(), connection, headers, body)

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 400
    assert "Signature verification failed" not in detail
    assert "not the JSON" in detail
    audit_event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "webhook.rejected")
    )
    assert audit_event is not None
    assert audit_event.metadata_json["reason"] == "malformed_body"


async def test_a_delivery_missing_its_identifying_headers_says_which_part_failed(
    session: AsyncSession, crypto: SecretCrypto, github_connection: tuple[Connection, str]
) -> None:
    connection, secret = github_connection
    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = {"X-Hub-Signature-256": sign_payload(secret, body)}

    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(), connection, headers, body)

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 400
    assert "Signature verification failed" not in detail
    assert "missing the headers" in detail
    audit_event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "webhook.rejected")
    )
    assert audit_event is not None
    assert audit_event.metadata_json["reason"] == "missing_headers"


def _github_case(secret: str, case: str) -> tuple[Mapping[str, str], bytes]:
    body = json.dumps(ISSUE_PAYLOAD).encode()
    if case == "bad_signature":
        return {"X-Hub-Signature-256": "sha256=" + "0" * 64}, body
    if case == "no_headers":
        return {"X-Hub-Signature-256": sign_payload(secret, body)}, body
    broken = b"{not json"
    return dict(github_headers(secret, broken, event="issues", delivery="d-x")), broken


def _linear_case(secret: str, case: str) -> tuple[Mapping[str, str], bytes]:
    from jhin_connectors.linear.webhook import sign_payload as sign_linear

    payload: dict[str, Any] = {"action": "create", "type": "Issue", "data": {}}
    if case != "stale_timestamp":
        payload["webhookTimestamp"] = 0
    body = json.dumps(payload).encode() if case != "bad_body" else b"{not json"
    headers = {"Linear-Signature": sign_linear(secret, body)}
    if case != "no_headers":
        headers |= {"Linear-Event": "Issue", "Linear-Delivery": "d-1"}
    return headers, body


def _vercel_case(secret: str, case: str) -> tuple[Mapping[str, str], bytes]:
    body = b"{not json" if case == "bad_body" else json.dumps({"payload": {}}).encode()
    return {"x-vercel-signature": sign_vercel_payload(secret, body)}, body


@pytest.mark.parametrize(
    ("connector_type", "case", "expected"),
    [
        ("github", "bad_signature", "verification_failed"),
        ("github", "no_headers", "missing_headers"),
        ("github", "bad_body", "malformed_body"),
        ("linear", "no_headers", "missing_headers"),
        ("linear", "bad_body", "malformed_body"),
        ("linear", "stale_timestamp", "stale_timestamp"),
        ("vercel", "bad_body", "malformed_body"),
        ("vercel", "no_identity", "malformed_body"),
    ],
)
def test_every_connector_refusal_is_classified_by_what_actually_failed(
    connector_type: str, case: str, expected: str
) -> None:
    """The classifier reads the connector's own message, so this pins the real
    wording of every refusal each connector raises. A connector that rewords
    one fails here rather than quietly regressing to "bad signature"."""
    secret = "s3cr3t-for-classification"
    builders = {"github": _github_case, "linear": _linear_case, "vercel": _vercel_case}
    headers, body = builders[connector_type](secret, case)
    connector = default_registry().get(connector_type)
    assert connector is not None

    with pytest.raises(WebhookVerificationError) as excinfo:
        connector.parse_webhook(headers, body, secret)

    assert webhooks._classify(excinfo.value).reason == expected


async def test_webhook_rejection_audit_does_not_persist_echoed_secret_or_body(
    session: AsyncSession,
    crypto: SecretCrypto,
    github_connection: tuple[Connection, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, secret = github_connection
    raw_body_marker = "raw-body-must-not-persist"
    body = json.dumps({"marker": raw_body_marker}).encode()
    registry = SingleConnectorRegistry(EchoingVerificationConnector())
    monkeypatch.setattr(webhooks, "default_registry", lambda: registry)

    with pytest.raises(HTTPException) as excinfo:
        await deliver(session, crypto, RecordingJetStream(), connection, {}, body)

    assert excinfo.value.status_code == 401
    audit_event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "webhook.rejected")
    )
    assert audit_event is not None
    assert audit_event.metadata_json == {
        "connector_type": "github",
        "reason": "verification_failed",
    }
    serialized_audit = json.dumps(audit_event.metadata_json)
    assert secret not in serialized_audit
    assert raw_body_marker not in serialized_audit


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
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    # Row rolled back: the provider's retry will process cleanly.
    await session.refresh(connection)  # rollback expired the ORM row
    assert (await session.scalars(select(WebhookDelivery))).all() == []

    js = RecordingJetStream()
    result = await deliver(session, crypto, js, connection, headers, body)
    assert result.outcome == "accepted"
    assert len(js.published) == 1


async def test_vercel_post_publish_precommit_retry_keeps_one_canonical_event(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "vercel-webhook-secret-123"
    connection, generated_secret = await connections_service.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="vercel",
        name="Vercel production",
        auth_type="access_token",
        credentials={"token": "fake-vercel-access-token"},
        config={},
        **REQ,
    )
    assert generated_secret is None
    await connections_service.set_webhook_secret(
        session,
        crypto,
        admin_ctx,
        connection.id,
        secret=secret,
        **REQ,
    )

    agent = Agent(
        workspace_id=admin_ctx.workspace_id,
        name="Release engineer",
        slug="release-engineer",
        status=AgentStatus.ACTIVE.value,
    )
    session.add(agent)
    await session.flush()
    trigger = Trigger(
        workspace_id=admin_ctx.workspace_id,
        name="Review a ready deployment",
        connection_id=connection.id,
        event_type="connector.vercel.deployment.ready",
        filter_json={},
        target_agent_id=agent.id,
    )
    session.add(trigger)
    await session.commit()

    connection_id = connection.id
    public_id = connection.public_id
    trigger_id = trigger.id
    body = json.dumps(
        {
            "id": "evt_commit_crash",
            "type": "deployment.ready",
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
        separators=(",", ":"),
    ).encode()
    headers = {"x-vercel-signature": sign_vercel_payload(secret, body)}
    ingress_js = RecordingJetStream()

    original_commit = session.commit

    async def fail_before_commit() -> None:
        raise RuntimeError("injected pre-commit failure")

    monkeypatch.setattr(session, "commit", fail_before_commit)
    with pytest.raises(HTTPException) as excinfo:
        await webhooks.process_delivery(
            session,
            crypto,
            ingress_js,
            connector_type="vercel",
            public_id=public_id,
            headers=headers,
            body=body,
            **REQ,
        )
    assert excinfo.value.status_code == 503
    monkeypatch.setattr(session, "commit", original_commit)

    bind = session.bind
    assert bind is not None
    fresh_sessions = async_sessionmaker(bind, expire_on_commit=False)
    async with fresh_sessions() as fresh_session:
        retry = await webhooks.process_delivery(
            fresh_session,
            crypto,
            ingress_js,
            connector_type="vercel",
            public_id=public_id,
            headers=headers,
            body=body,
            **REQ,
        )
        deliveries = list(await fresh_session.scalars(select(WebhookDelivery)))

    assert retry.outcome == "accepted"
    assert len(deliveries) == 1
    assert deliveries[0].connection_id == connection_id
    assert len(ingress_js.published) == 2
    ingress_envelopes = [
        EventEnvelope.from_bytes(payload) for _, payload, _ in ingress_js.published
    ]
    assert ingress_envelopes[0].event_id == ingress_envelopes[1].event_id
    assert ingress_envelopes[0].event_id == webhooks.ingress_event_id(
        "vercel", connection_id, "evt_commit_crash"
    )
    assert {
        published_headers["Nats-Msg-Id"] for _, _, published_headers in ingress_js.published
    } == {str(ingress_envelopes[0].event_id)}

    canonical_js = DeduplicatingRecordingJetStream()
    normalizer = IngressNormalizer(canonical_js)  # type: ignore[arg-type]
    ingress_messages: list[IngressMessage] = []
    for subject, payload, _ in ingress_js.published:
        message = IngressMessage(subject, payload)
        ingress_messages.append(message)
        await normalizer.handle(message)  # type: ignore[arg-type]

    assert all(message.acked and not message.termed for message in ingress_messages)
    assert len(canonical_js.published) == 1
    canonical_subject, canonical_payload, canonical_headers = canonical_js.published[0]
    canonical = EventEnvelope.from_bytes(canonical_payload)
    assert canonical_subject.endswith(".connector.vercel.deployment.ready")
    assert canonical.event_id == derived_event_id(ingress_envelopes[0].event_id, 0)
    assert canonical_headers["Nats-Msg-Id"] == str(canonical.event_id)
    assert "must-not-survive" not in canonical_payload.decode()

    temporal = RecordingTemporal()
    matcher = TriggerMatcher(
        fresh_sessions,
        temporal,  # type: ignore[arg-type]
        metrics=noop_metrics(),
        tracer=noop_tracer(),
        cache_ttl_seconds=0.0,
    )
    await matcher.handle_event(canonical)
    # A downstream consumer redelivery remains safe even beyond JetStream's
    # duplicate suppression.
    await matcher.handle_event(canonical)

    assert len(temporal.calls) == 1
    async with fresh_sessions() as fresh_session:
        invocations = list(
            await fresh_session.scalars(
                select(TriggerInvocation).where(TriggerInvocation.trigger_id == trigger_id)
            )
        )
    assert sorted(invocation.status for invocation in invocations) == [
        "duplicate",
        "started",
    ]
