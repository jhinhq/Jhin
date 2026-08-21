"""API observability boundary tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from opentelemetry import baggage, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

import jhin_api.main as main_module
from jhin_api.main import create_app
from jhin_api.settings import Settings
from jhin_observability import (
    ObservabilityNotInitializedError,
    ObservabilitySettings,
    get_runtime,
    noop_tracer,
)
from jhin_observability import (
    safe_span as real_safe_span,
)


def test_settings() -> Settings:
    return Settings(
        app_env="test",
        log_level="INFO",
        database_url="sqlite+aiosqlite:///:memory:",
        otel_exporter_otlp_endpoint=None,
        otel_exporter_otlp_insecure=False,
        otel_traces_sampler="always_on",
    )


test_settings.__test__ = False


@dataclass
class _FakeRuntime:
    events: list[str]
    tracer: Any = field(default_factory=noop_tracer)

    def shutdown(self, timeout_millis: int = 5_000) -> None:
        assert timeout_millis == 5_000
        self.events.append("runtime.shutdown")


class _FakeEngine:
    def __init__(self, events: list[str], *, fail_dispose: bool = False) -> None:
        self._events = events
        self._fail_dispose = fail_dispose

    async def dispose(self) -> None:
        self._events.append("engine.dispose")
        if self._fail_dispose:
            raise RuntimeError("engine cleanup canary")


class _FakeNats:
    is_closed = False

    def __init__(self, events: list[str], *, fail_close: bool = False) -> None:
        self._events = events
        self._fail_close = fail_close

    async def close(self) -> None:
        self._events.append("nats.close")
        if self._fail_close:
            raise RuntimeError("nats cleanup canary")


class _RecordingExporter(InMemorySpanExporter):
    shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True
        super().shutdown()


@dataclass
class _ApiTelemetry:
    provider: TracerProvider
    tracer: Tracer
    exporter: _RecordingExporter
    caller_tracers: list[Tracer]


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Any:
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": "api-test",
                "service.version": "test",
                "deployment.environment": "test",
            }
        )
    )
    exporter = _RecordingExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("api-tests")
    caller_tracers: list[Tracer] = []

    def recording_safe_span(
        name: str,
        *,
        tracer: Tracer | None = None,
        **kwargs: Any,
    ) -> Any:
        assert tracer is not None
        caller_tracers.append(tracer)
        return real_safe_span(name, tracer=test_tracer, **kwargs)

    monkeypatch.setattr(main_module, "safe_span", recording_safe_span, raising=False)
    value = _ApiTelemetry(provider, test_tracer, exporter, caller_tracers)
    try:
        yield value
    finally:
        provider.shutdown()
        assert exporter.shutdown_called is True


@pytest.fixture
def spans(telemetry: _ApiTelemetry) -> InMemorySpanExporter:
    return telemetry.exporter


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch,
    telemetry: _ApiTelemetry,
) -> FastAPI:
    del telemetry
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    return create_app(test_settings())


@pytest.fixture
async def client(
    app: FastAPI,
    telemetry: _ApiTelemetry,
) -> Any:
    async with app.router.lifespan_context(app):
        runtime = app.state.observability
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as http_client:
            yield http_client
        assert telemetry.caller_tracers
        assert all(tracer is runtime.tracer for tracer in telemetry.caller_tracers)
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


def _serialized_span(span: ReadableSpan) -> str:
    parent = span.parent
    return json.dumps(
        {
            "name": span.name,
            "context": {
                "trace_id": format(span.context.trace_id, "032x") if span.context else None,
                "span_id": format(span.context.span_id, "016x") if span.context else None,
            },
            "parent": {
                "trace_id": format(parent.trace_id, "032x") if parent else None,
                "span_id": format(parent.span_id, "016x") if parent else None,
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


def _server_spans(spans: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in spans.get_finished_spans() if span.name == "http.server.request"]


def _json_records(
    captured: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> tuple[list[dict[str, Any]], str]:
    streams = captured.readouterr()
    records = [
        json.loads(line) for line in streams.out.splitlines() if line.lstrip().startswith("{")
    ]
    records.extend(record.msg for record in caplog.records if isinstance(record.msg, dict))
    return records, streams.out + streams.err


def _direct_scope(*, method: str = "GET", route: APIRoute | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/api/_test/direct",
        "raw_path": b"/api/_test/direct",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": None,
        "server": ("test", 80),
        "state": {},
        "route": route,
    }


async def _unused_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_settings_owns_observability_configuration() -> None:
    assert isinstance(test_settings(), ObservabilitySettings)


def test_app_factory_constructs_no_runtime_or_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("resource initialized before lifespan")

    monkeypatch.setattr(main_module, "initialize_observability", forbidden, raising=False)
    monkeypatch.setattr(main_module, "_load_secret_crypto", forbidden)
    monkeypatch.setattr(main_module, "create_engine", forbidden)

    app = main_module.create_app(test_settings())

    assert not hasattr(app.state, "observability")
    assert not hasattr(app.state, "secret_crypto")
    assert not hasattr(app.state, "engine")


@pytest.mark.asyncio
async def test_lifespan_owns_exactly_one_runtime() -> None:
    app = create_app(test_settings())
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()

    async with app.router.lifespan_context(app):
        runtime = app.state.observability
        assert get_runtime() is runtime
        assert app.state.engine is not None

    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_cleanup", ["nats", "engine"])
async def test_lifespan_cleanup_is_ordered_and_failure_contained(
    monkeypatch: pytest.MonkeyPatch, failing_cleanup: str
) -> None:
    events: list[str] = []
    runtime = _FakeRuntime(events)
    engine = _FakeEngine(events, fail_dispose=failing_cleanup == "engine")

    def initialize(config: Any) -> _FakeRuntime:
        assert config.service_name == "api"
        assert config.service_version == "test-service-version"
        events.append("runtime.initialize")
        return runtime

    def load_secret_crypto() -> object:
        events.append("secret.load")
        return object()

    def build_engine(database_url: str, *, trace_sql: bool, tracer: Any) -> _FakeEngine:
        assert database_url == "sqlite+aiosqlite:///:memory:"
        assert trace_sql is True
        assert tracer is runtime.tracer
        events.append("engine.create")
        return engine

    class _Logger:
        def info(self, event: str, **kwargs: object) -> None:
            events.append(event)

    monkeypatch.setattr(main_module, "initialize_observability", initialize, raising=False)
    monkeypatch.setattr(
        main_module, "service_version", lambda _: "test-service-version", raising=False
    )
    monkeypatch.setattr(main_module, "_load_secret_crypto", load_secret_crypto)
    monkeypatch.setattr(main_module, "create_engine", build_engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda _: object())
    monkeypatch.setattr(main_module, "logger", _Logger())

    app = main_module.create_app(test_settings())
    with pytest.raises(RuntimeError, match=f"{failing_cleanup} cleanup canary"):
        async with app.router.lifespan_context(app):
            app.state.nats_client = _FakeNats(events, fail_close=failing_cleanup == "nats")

    assert events[:3] == ["runtime.initialize", "secret.load", "engine.create"]
    assert events[-4:] == [
        "nats.close",
        "engine.dispose",
        "api.stopped",
        "runtime.shutdown",
    ]


@pytest.mark.asyncio
async def test_failure_after_runtime_creation_still_shuts_runtime_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = _FakeRuntime(events)

    monkeypatch.setattr(
        main_module,
        "initialize_observability",
        lambda config: events.append("runtime.initialize") or runtime,
        raising=False,
    )
    monkeypatch.setattr(
        main_module, "service_version", lambda _: "test-service-version", raising=False
    )

    def fail_secret_load() -> None:
        events.append("secret.load")
        raise RuntimeError("secret setup canary")

    monkeypatch.setattr(main_module, "_load_secret_crypto", fail_secret_load)

    app = main_module.create_app(test_settings())
    with pytest.raises(RuntimeError, match="secret setup canary"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan yielded after setup failure")

    assert events == ["runtime.initialize", "secret.load", "runtime.shutdown"]


@pytest.mark.asyncio
async def test_api_uses_valid_parent_returns_request_id_and_discards_baggage(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    path_canary = "path-canary-4b61"
    query_canary = "query-canary-055f"
    header_canary = "header-canary-b735"
    body_canary = "body-canary-92f3"
    baggage_canary = "baggage-canary-7d8b"

    async def probe(request: Request, raw: str) -> dict[str, str]:
        del raw
        assert baggage.get_all() == {}
        await request.body()
        return {"status": "ok"}

    app.add_api_route("/api/_test/probe/{raw}", probe, methods=["POST"])
    capsys.readouterr()
    response = await client.post(
        f"/api/_test/probe/{path_canary}?search={query_canary}",
        headers={
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "baggage": f"workspace_id={baggage_canary},metric_label=foreign",
            "x-canary": header_canary,
        },
        content=body_canary,
    )

    assert response.status_code == 200
    assert UUID(response.headers["X-Request-ID"])
    server = _server_spans(spans)[0]
    assert format(server.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert dict(server.attributes or {}) == {
        "http.request.method": "POST",
        "http.route": "/api/:path*",
        "http.response.status_code": 200,
        "http.response.status_class": "2xx",
    }
    records, raw_logs = _json_records(capsys, caplog)
    finished = [record for record in records if record.get("event") == "api.request_finished"]
    assert len(finished) == 1
    assert finished[0]["request_id"] == response.headers["X-Request-ID"]
    assert finished[0]["http_route"] == "/api/:path*"
    serialized = _serialized_span(server) + json.dumps(records, sort_keys=True) + raw_logs
    for canary in (
        path_canary,
        query_canary,
        header_canary,
        body_canary,
        baggage_canary,
        "foreign",
    ):
        assert canary not in serialized
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_invalid_traceparent_creates_new_root(
    client: httpx.AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get("/api/v1/health", headers={"traceparent": "attacker-value"})

    assert response.status_code == 200
    server = _server_spans(spans)[0]
    assert server.parent is None
    assert dict(server.attributes or {})["http.route"] == "/api/:path*"
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_unmatched_route_is_other(
    client: httpx.AsyncClient, spans: InMemorySpanExporter
) -> None:
    response = await client.get("/missing/path-canary-must-not-export")

    assert response.status_code == 404
    server = _server_spans(spans)[0]
    assert dict(server.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "other",
        "http.response.status_code": 404,
        "http.response.status_class": "4xx",
    }


def test_route_normalizer_collapses_every_registered_api_template(app: FastAPI) -> None:
    normalize = main_module.normalize_http_route

    def collect(routes: list[Any]) -> list[APIRoute]:
        collected: list[APIRoute] = []
        for route in routes:
            if isinstance(route, APIRoute):
                collected.append(route)
            else:
                nested = getattr(route, "routes", None)
                if nested is None:
                    nested = getattr(getattr(route, "original_router", None), "routes", None)
                if isinstance(nested, list):
                    collected.extend(collect(nested))
        return collected

    registered = collect(app.routes)
    assert registered
    assert {normalize({"route": route}) for route in registered} <= {"/api/:path*", "other"}
    assert all(
        normalize({"route": route}) == "/api/:path*"
        for route in registered
        if route.path.startswith("/api/")
    )

    async def endpoint() -> Response:
        return Response()

    cases = [
        ({}, "other"),
        ({"route": object()}, "other"),
        ({"route": APIRoute("/internal", endpoint=endpoint)}, "other"),
        ({"route": APIRoute("/api/" + "x" * 201, endpoint=endpoint)}, "other"),
    ]
    for scope, expected in cases:
        assert normalize(scope) == expected


@pytest.mark.asyncio
async def test_pre_start_exception_returns_one_generic_safe_500(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del client
    exception_canary = "pre-start-exception-canary-10e9"

    async def fail() -> None:
        raise RuntimeError(exception_canary)

    app.add_api_route("/api/_test/fail", fail)
    capsys.readouterr()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as unsafe_client:
        response = await unsafe_client.get("/api/_test/fail")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert UUID(response.headers["X-Request-ID"])
    server = _server_spans(spans)[0]
    assert dict(server.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/api/:path*",
        "http.response.status_code": 500,
        "http.response.status_class": "5xx",
        "error.type": "RuntimeError",
        "error.code": "internal_error",
    }
    assert server.events == ()
    assert server.status.description is None
    records, raw_logs = _json_records(capsys, caplog)
    serialized = (
        response.content.decode()
        + _serialized_span(server)
        + json.dumps(records, sort_keys=True)
        + raw_logs
    )
    assert exception_canary not in serialized
    assert len([record for record in records if record.get("event") == "api.request_failed"]) == 1
    assert len([record for record in records if record.get("event") == "api.request_finished"]) == 1
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_non_http_scope_is_forwarded_without_telemetry(
    telemetry: _ApiTelemetry, spans: InMemorySpanExporter
) -> None:
    calls: list[Scope] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        calls.append(scope)

    middleware = main_module.HttpObservabilityMiddleware(downstream)
    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    await middleware(scope, _unused_receive, lambda message: asyncio.sleep(0))

    assert calls == [scope]
    assert scope["state"] == {}
    assert _server_spans(spans) == []
    assert telemetry.caller_tracers == []


@pytest.mark.asyncio
async def test_duplicate_request_id_headers_are_replaced_by_one_canonical_header(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
) -> None:
    del client
    messages: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"x-request-id", b"downstream-one"),
                    (b"X-REQUEST-ID", b"downstream-two"),
                    (b"content-type", b"text/plain"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def record(message: Message) -> None:
        messages.append(message)

    route = APIRoute("/api/_test/direct", endpoint=lambda: Response())
    scope = _direct_scope(route=route)
    scope["app"] = app
    middleware = main_module.HttpObservabilityMiddleware(downstream)
    await middleware(scope, _unused_receive, record)

    start = messages[0]
    headers = start["headers"]
    request_headers = [item for item in headers if item[0].lower() == b"x-request-id"]
    assert len(request_headers) == 1
    assert request_headers[0][0] == b"X-Request-ID"
    assert UUID(request_headers[0][1].decode("ascii"))
    assert scope["state"]["request_id"] == UUID(request_headers[0][1].decode("ascii"))
    assert len(_server_spans(spans)) == 1


@pytest.mark.asyncio
async def test_streaming_lifecycle_keeps_one_context_until_background_completion(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del client
    body_canary = b"stream-body-canary-87f0"
    messages: list[Message] = []
    observed_request_ids: list[str] = []

    def observe(scope: Scope) -> None:
        current = trace.get_current_span()
        assert current.is_recording()
        request_id = str(scope["state"]["request_id"])
        assert structlog.contextvars.get_contextvars()["request_id"] == request_id
        observed_request_ids.append(request_id)
        assert _server_spans(spans) == []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        observe(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        observe(scope)
        await send({"type": "http.response.body", "body": body_canary, "more_body": True})
        observe(scope)
        await send({"type": "http.response.body", "body": b"done", "more_body": False})
        await asyncio.sleep(0)
        observe(scope)

    async def record(message: Message) -> None:
        messages.append(message)

    route = APIRoute("/api/_test/stream", endpoint=lambda: Response())
    scope = _direct_scope(route=route)
    scope["app"] = app
    capsys.readouterr()
    middleware = main_module.HttpObservabilityMiddleware(downstream)
    await middleware(scope, _unused_receive, record)

    assert len(set(observed_request_ids)) == 1
    server = _server_spans(spans)[0]
    records, raw_logs = _json_records(capsys, caplog)
    assert body_canary.decode() not in (
        _serialized_span(server) + json.dumps(records, sort_keys=True) + raw_logs
    )
    assert len([record for record in records if record.get("event") == "api.request_finished"]) == 1
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_post_start_streaming_failure_reraises_without_second_response(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del client
    exception_canary = "stream-exception-canary-f89d"
    messages: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError(exception_canary)

    async def record(message: Message) -> None:
        messages.append(message)

    route = APIRoute("/api/_test/stream", endpoint=lambda: Response())
    scope = _direct_scope(route=route)
    scope["app"] = app
    capsys.readouterr()
    middleware = main_module.HttpObservabilityMiddleware(downstream)
    with pytest.raises(RuntimeError, match=exception_canary):
        await middleware(scope, _unused_receive, record)

    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    server = _server_spans(spans)[0]
    assert dict(server.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/api/:path*",
        "http.response.status_code": 200,
        "http.response.status_class": "2xx",
        "error.type": "RuntimeError",
        "error.code": "internal_error",
    }
    assert server.events == ()
    assert server.status.description is None
    records, raw_logs = _json_records(capsys, caplog)
    assert exception_canary not in (
        _serialized_span(server) + json.dumps(records, sort_keys=True) + raw_logs
    )
    assert len([record for record in records if record.get("event") == "api.request_failed"]) == 1
    assert len([record for record in records if record.get("event") == "api.request_finished"]) == 1
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["pre_start", "body_send"])
async def test_cancellation_propagates_and_cleans_context(
    app: FastAPI,
    client: httpx.AsyncClient,
    spans: InMemorySpanExporter,
    boundary: str,
) -> None:
    del client
    messages: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        if boundary == "pre_start":
            raise asyncio.CancelledError
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"cancel-boundary"})

    async def record(message: Message) -> None:
        if boundary == "body_send" and message["type"] == "http.response.body":
            raise asyncio.CancelledError
        messages.append(message)

    route = APIRoute("/api/_test/cancel", endpoint=lambda: Response())
    scope = _direct_scope(route=route)
    scope["app"] = app
    middleware = main_module.HttpObservabilityMiddleware(downstream)
    with pytest.raises(asyncio.CancelledError):
        await middleware(scope, _unused_receive, record)

    server = _server_spans(spans)[0]
    status = 500 if boundary == "pre_start" else 200
    assert dict(server.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/api/:path*",
        "http.response.status_code": status,
        "http.response.status_class": f"{status // 100}xx",
    }
    assert server.events == ()
    assert server.status.description is None
    assert structlog.contextvars.get_contextvars() == {}
    assert not trace.get_current_span().is_recording()
