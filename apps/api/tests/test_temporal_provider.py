"""One interceptor-aware API Temporal provider and lifespan ownership."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from types import SimpleNamespace, TracebackType
from typing import Any, cast, get_type_hints

import httpx
import pytest
from fastapi import Request
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.client import Client as TemporalClient

from jhin_api.settings import Settings
from jhin_observability import ObservabilityRuntime


def _module() -> Any:
    return importlib.import_module("jhin_api.temporal")


def _settings() -> Settings:
    return Settings(
        app_env="test",
        app_name="Jhin",
        app_url="http://test",
        database_url="sqlite+aiosqlite:///:memory:",
        nats_url="nats://127.0.0.1:4222",
        temporal_address="temporal.private:7233",
        temporal_namespace="private-namespace",
        otel_exporter_otlp_endpoint=None,
        otel_exporter_otlp_insecure=False,
        otel_traces_sampler="always_on",
    )


@pytest.mark.asyncio
async def test_provider_connects_once_under_concurrency_and_forwards_exact_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    runtime = cast(ObservabilityRuntime, SimpleNamespace())
    expected_interceptors = [object()]
    expected_client = cast(TemporalClient, object())
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def connect(*args: object, **kwargs: object) -> TemporalClient:
        calls.append((args, dict(kwargs)))
        entered.set()
        await release.wait()
        return expected_client

    builders: list[object] = []

    def build(value: object) -> list[object]:
        builders.append(value)
        return expected_interceptors

    monkeypatch.setattr(module, "temporal_client_interceptors", build)
    monkeypatch.setattr(module.Client, "connect", connect)
    provider = module.TemporalClientProvider(_settings(), runtime)
    first = asyncio.create_task(provider.get())
    await entered.wait()
    second = asyncio.create_task(provider.get())
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    assert await first is expected_client
    assert await second is expected_client
    assert await provider.get() is expected_client
    assert builders == [runtime]
    args, kwargs = calls[0]
    assert args == ("temporal.private:7233",)
    assert kwargs == {
        "namespace": "private-namespace",
        "interceptors": expected_interceptors,
    }
    assert kwargs["interceptors"] is expected_interceptors


def test_provider_public_contract_is_exact() -> None:
    module = _module()
    signature = inspect.signature(module.TemporalClientProvider)
    assert tuple(signature.parameters) == ("settings", "observability")
    assert get_type_hints(module.TemporalClientProvider.get)["return"] is TemporalClient
    get_signature = inspect.signature(module.TemporalClientProvider.get)
    assert tuple(get_signature.parameters) == ("self",)


@pytest.mark.asyncio
async def test_business_dependency_uses_app_state_provider_identity() -> None:
    from jhin_api.deps import get_temporal_client

    client = cast(TemporalClient, object())

    class Provider:
        calls = 0

        async def get(self) -> TemporalClient:
            self.calls += 1
            return client

    provider = Provider()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(temporal_provider=provider))),
    )
    assert await get_temporal_client(request) is client
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_app_temporal_provider_graph_is_singleton_and_privacy_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jhin_api.deps as deps_module
    import jhin_api.main as main_module
    from jhin_api.health import service

    address_canary = "temporal-address-private-canary:7233"
    rpc_canary = "temporal-rpc-private-canary"
    check_canary = "temporal-check-private-canary"
    canaries = (address_canary, rpc_canary, check_canary)
    settings = _settings().model_copy(update={"temporal_address": address_canary})
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tracer_provider.get_tracer("api-temporal-provider-graph-test")
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=tracer,
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )

    class ServiceClient:
        calls = 0

        async def check_health(self) -> bool:
            self.calls += 1
            raise RuntimeError(check_canary)

        def __repr__(self) -> str:
            return rpc_canary

    service_client = ServiceClient()

    class Client:
        def __init__(self) -> None:
            self.service_client = service_client

        def __repr__(self) -> str:
            return rpc_canary

    temporal_client = cast(TemporalClient, Client())
    module = _module()
    provider = module.TemporalClientProvider(settings, runtime)
    connect_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def connect(*args: object, **kwargs: object) -> TemporalClient:
        connect_calls.append((args, dict(kwargs)))
        return temporal_client

    monkeypatch.setattr(module.Client, "connect", connect)
    provider_get_calls: list[object] = []
    original_get = provider.get

    async def recording_get() -> TemporalClient:
        provider_get_calls.append(provider)
        return await original_get()

    monkeypatch.setattr(provider, "get", recording_get)
    provider_factory_calls: list[tuple[object, object]] = []

    def provider_factory(received_settings: object, received_runtime: object) -> object:
        provider_factory_calls.append((received_settings, received_runtime))
        return provider

    class Engine:
        async def dispose(self) -> None:
            return None

    engine = Engine()
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "TemporalClientProvider", provider_factory)
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    monkeypatch.setattr(main_module, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda _engine: object())

    structured_logs: list[dict[str, object]] = []

    class RecordingLogger:
        def _record(self, level: str, event: str, **fields: object) -> None:
            structured_logs.append({"level": level, "event": event, **fields})

        def info(self, event: str, **fields: object) -> None:
            self._record("info", event, **fields)

        def warning(self, event: str, **fields: object) -> None:
            self._record("warning", event, **fields)

        def error(self, event: str, **fields: object) -> None:
            self._record("error", event, **fields)

    monkeypatch.setattr(main_module, "logger", RecordingLogger())

    postgres_calls: list[object] = []
    nats_calls: list[str] = []

    async def check_postgres(received: object) -> None:
        postgres_calls.append(received)

    async def check_nats(received: str) -> None:
        nats_calls.append(received)

    monkeypatch.setattr(service, "check_postgres", check_postgres)
    monkeypatch.setattr(service, "check_nats", check_nats)
    readiness_calls: list[tuple[object, object, object]] = []
    check_temporal_calls: list[object] = []
    original_readiness = service.readiness
    original_check_temporal = service.check_temporal

    async def recording_readiness(
        received_settings: object,
        received_engine: object,
        received_provider: object,
    ) -> object:
        readiness_calls.append((received_settings, received_engine, received_provider))
        return await original_readiness(
            cast(Settings, received_settings),
            cast(Any, received_engine),
            cast(Any, received_provider),
        )

    async def recording_check_temporal(received_provider: object) -> None:
        check_temporal_calls.append(received_provider)
        await original_check_temporal(cast(Any, received_provider))

    monkeypatch.setattr(service, "readiness", recording_readiness)
    monkeypatch.setattr(service, "check_temporal", recording_check_temporal)
    caplog.clear()
    capsys.readouterr()

    app = main_module.create_app(settings)
    with caplog.at_level(logging.DEBUG):
        async with app.router.lifespan_context(app):
            assert app.state.temporal_provider is provider
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/health/ready")
            request = cast(Request, SimpleNamespace(app=app))
            business_client = await deps_module.get_temporal_client(request)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["app"] == settings.app_name
    dependencies = {item["name"]: item for item in body["dependencies"]}
    assert set(dependencies) == {"postgres", "nats", "temporal"}
    assert dependencies["postgres"]["status"] == "ok"
    assert dependencies["nats"]["status"] == "ok"
    assert dependencies["temporal"]["status"] == "error"
    assert dependencies["temporal"]["detail"] == (
        "TemporalHealthUnavailable: Temporal workflow service is unavailable"
    )
    assert provider_factory_calls == [(settings, runtime)]
    assert readiness_calls == [(settings, engine, provider)]
    assert check_temporal_calls == [provider]
    assert postgres_calls == [engine]
    assert nats_calls == [settings.nats_url]
    assert provider_get_calls == [provider, provider]
    assert len(connect_calls) == 1
    connect_args, connect_kwargs = connect_calls[0]
    assert connect_args == (address_canary,)
    assert connect_kwargs["namespace"] == settings.temporal_namespace
    assert isinstance(connect_kwargs["interceptors"], list)
    assert service_client.calls == 1
    assert provider._client is temporal_client
    assert business_client is temporal_client
    assert shutdowns == [5_000]

    captured = capsys.readouterr()
    rendered_logs = (
        json.dumps(structured_logs, default=str)
        + captured.out
        + captured.err
        + "\n".join(record.getMessage() for record in caplog.records)
    )
    rendered_spans = repr(
        [
            (
                span.name,
                dict(span.attributes or {}),
                [(event.name, dict(event.attributes or {})) for event in span.events],
                span.status.description,
            )
            for span in exporter.get_finished_spans()
        ]
    )
    assert exporter.get_finished_spans()
    private_surfaces = response.text + rendered_logs + rendered_spans
    for canary in canaries:
        assert canary not in private_surfaces


def test_protected_health_handoff_keeps_the_same_provider_contract() -> None:
    module = _module()
    from jhin_api.health import service

    hints = get_type_hints(service.check_temporal)
    assert hints["provider"] is module.TemporalClientProvider
    assert "return" in hints


@pytest.mark.asyncio
async def test_api_lifespan_assigns_exact_runtime_and_provider_and_shuts_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.main as main_module

    module = _module()
    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )
    provider_instances: list[tuple[object, object, object]] = []

    class Provider:
        def __init__(self, settings: object, observability: object) -> None:
            provider_instances.append((self, settings, observability))

    engine = SimpleNamespace(dispose=lambda: None)

    async def dispose() -> None:
        return None

    engine.dispose = dispose
    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "TemporalClientProvider", Provider)
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    monkeypatch.setattr(main_module, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda _engine: object())
    app = main_module.create_app(_settings())
    async with app.router.lifespan_context(app):
        assert app.state.observability is runtime
        assert app.state.temporal_provider is provider_instances[0][0]
        assert provider_instances[0][1:] == (_settings(), runtime)
    assert shutdowns == [5_000]
    assert module.TemporalClientProvider is not Provider


@pytest.mark.asyncio
async def test_provider_construction_failure_still_shuts_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.main as main_module

    shutdowns: list[int] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            shutdown=lambda timeout_millis: shutdowns.append(timeout_millis),
        ),
    )

    class Failure(RuntimeError):
        pass

    failure = Failure("provider-private-canary")

    def fail_provider(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "TemporalClientProvider", fail_provider)
    app = main_module.create_app(_settings())
    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            pass
    except BaseException as exc:
        caught = exc
    assert caught is failure
    assert shutdowns == [5_000]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_stage",
    ["engine", "session_factory", "nats_connect_lock"],
)
async def test_api_lifespan_acquisition_failure_cleans_only_acquired_resources(
    failed_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.main as main_module

    events: list[str] = []
    original_tracebacks: list[TracebackType] = []
    failure = RuntimeError(f"{failed_stage}-acquisition-private-canary")

    def raise_failure() -> None:
        try:
            raise failure
        except BaseException as error:
            assert error.__traceback__ is not None
            original_tracebacks.append(error.__traceback__)
            raise

    class Runtime:
        tracer = object()

        def shutdown(self, *, timeout_millis: int) -> None:
            events.append(f"runtime.shutdown:{timeout_millis}")

    runtime = Runtime()

    class Provider:
        def __init__(self, settings: object, observability: object) -> None:
            assert settings == _settings()
            assert observability is runtime
            self.client: object | None = None
            self.get_calls = 0
            events.append("provider.create")

        async def get(self) -> object:
            self.get_calls += 1
            raise AssertionError("provider must stay lazy during lifespan acquisition")

    providers: list[Provider] = []

    def create_provider(settings: object, observability: object) -> Provider:
        provider = Provider(settings, observability)
        providers.append(provider)
        return provider

    class Engine:
        dispose_count = 0

        async def dispose(self) -> None:
            self.dispose_count += 1
            events.append("engine.dispose")

    engine = Engine()
    session_factory = object()
    secret_crypto = object()

    def initialize(_config: object) -> Runtime:
        events.append("runtime.initialize")
        return runtime

    def load_secret_crypto() -> object:
        events.append("secret.load")
        return secret_crypto

    def create_test_engine(*_args: object, **_kwargs: object) -> Engine:
        events.append("engine.create")
        if failed_stage == "engine":
            raise_failure()
        return engine

    def create_test_session_factory(received: object) -> object:
        assert received is engine
        events.append("session_factory.create")
        if failed_stage == "session_factory":
            raise_failure()
        return session_factory

    def create_lock() -> object:
        events.append("nats_connect_lock.create")
        if failed_stage == "nats_connect_lock":
            raise_failure()
        return object()

    monkeypatch.setattr(main_module, "initialize_observability", initialize)
    monkeypatch.setattr(main_module, "TemporalClientProvider", create_provider)
    monkeypatch.setattr(main_module, "_load_secret_crypto", load_secret_crypto)
    monkeypatch.setattr(main_module, "create_engine", create_test_engine)
    monkeypatch.setattr(
        main_module,
        "create_session_factory",
        create_test_session_factory,
    )
    app = main_module.create_app(_settings())
    monkeypatch.setattr(
        main_module,
        "asyncio",
        SimpleNamespace(CancelledError=asyncio.CancelledError, Lock=create_lock),
    )

    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            raise AssertionError("acquisition failure must prevent lifespan entry")
    except BaseException as error:
        caught = error

    assert caught is failure
    assert caught.__traceback__ is not None
    assert len(original_tracebacks) == 1

    def traceback_tail(traceback: TracebackType) -> TracebackType:
        while traceback.tb_next is not None:
            traceback = traceback.tb_next
        return traceback

    assert traceback_tail(caught.__traceback__) is traceback_tail(original_tracebacks[0])
    expected_events = [
        "runtime.initialize",
        "provider.create",
        "secret.load",
        "engine.create",
    ]
    if failed_stage != "engine":
        expected_events.append("session_factory.create")
    if failed_stage == "nats_connect_lock":
        expected_events.append("nats_connect_lock.create")
    if failed_stage != "engine":
        expected_events.append("engine.dispose")
    expected_events.append("runtime.shutdown:5000")
    assert events == expected_events
    assert engine.dispose_count == (0 if failed_stage == "engine" else 1)
    assert len(providers) == 1
    assert app.state.temporal_provider is providers[0]
    assert providers[0].client is None
    assert providers[0].get_calls == 0
    assert app.state.observability is runtime
    assert app.state.secret_crypto is secret_crypto
    assert getattr(app.state, "nats_client", None) is None
    assert not hasattr(app.state, "nats_connect_lock")
    if failed_stage == "engine":
        assert not hasattr(app.state, "engine")
        assert not hasattr(app.state, "session_factory")
    elif failed_stage == "session_factory":
        assert app.state.engine is engine
        assert not hasattr(app.state, "session_factory")
    else:
        assert app.state.engine is engine
        assert app.state.session_factory is session_factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_attribute",
    [
        "observability",
        "temporal_provider",
        "secret_crypto",
        "engine",
        "session_factory",
        "nats_client",
        "nats_connect_lock",
    ],
)
async def test_app_state_assignment_failure_still_shuts_exact_runtime(
    failed_attribute: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.main as main_module

    cleanup_order: list[str] = []
    runtime = cast(
        ObservabilityRuntime,
        SimpleNamespace(
            tracer=object(),
            shutdown=lambda timeout_millis: cleanup_order.append(
                f"runtime.shutdown:{timeout_millis}"
            ),
        ),
    )
    failure = RuntimeError(f"{failed_attribute}-assignment-private-canary")

    class Engine:
        async def dispose(self) -> None:
            cleanup_order.append("engine.dispose")

    engine = Engine()

    class State:
        def __setattr__(self, name: str, value: object) -> None:
            if name == failed_attribute:
                raise failure
            object.__setattr__(self, name, value)

    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: runtime)
    monkeypatch.setattr(main_module, "TemporalClientProvider", lambda *_args: object())
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    monkeypatch.setattr(main_module, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda received: object())
    app = main_module.create_app(_settings())
    app.state = State()
    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            pass
    except BaseException as exc:
        caught = exc
    assert caught is failure
    expected_cleanup = (
        ["runtime.shutdown:5000"]
        if failed_attribute in {"observability", "temporal_provider", "secret_crypto"}
        else ["engine.dispose", "runtime.shutdown:5000"]
    )
    assert cleanup_order == expected_cleanup


@pytest.mark.asyncio
async def test_assignment_failure_remains_authoritative_over_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.main as main_module

    assignment_error = RuntimeError("api-state-assignment-error")
    dispose_error = RuntimeError("api-engine-dispose-error")
    shutdown_error = RuntimeError("api-runtime-shutdown-error")
    cleanup_order: list[str] = []

    class Runtime:
        tracer = object()

        def shutdown(self, *, timeout_millis: int) -> None:
            cleanup_order.append(f"runtime.shutdown:{timeout_millis}")
            raise shutdown_error

    class Engine:
        async def dispose(self) -> None:
            cleanup_order.append("engine.dispose")
            raise dispose_error

    class State:
        def __setattr__(self, name: str, value: object) -> None:
            if name == "engine":
                raise assignment_error
            object.__setattr__(self, name, value)

    monkeypatch.setattr(main_module, "initialize_observability", lambda _config: Runtime())
    monkeypatch.setattr(main_module, "TemporalClientProvider", lambda *_args: object())
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)
    monkeypatch.setattr(main_module, "create_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(main_module, "create_session_factory", lambda _engine: object())
    app = main_module.create_app(_settings())
    app.state = State()

    caught: BaseException | None = None
    try:
        async with app.router.lifespan_context(app):
            pass
    except BaseException as error:
        caught = error
    assert caught is assignment_error
    assert cleanup_order == ["engine.dispose", "runtime.shutdown:5000"]


@pytest.mark.asyncio
async def test_lazy_nats_assignment_failure_closes_the_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_api.deps as deps_module

    assignment_error = RuntimeError("nats-state-assignment-error")
    close_count = 0

    class Client:
        is_closed = False

        def jetstream(self) -> object:
            return object()

        async def close(self) -> None:
            nonlocal close_count
            close_count += 1

    client = Client()

    class Lock:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class State:
        settings = SimpleNamespace(nats_url="nats://test")
        nats_connect_lock = Lock()
        _client: object | None = None

        @property
        def nats_client(self) -> object | None:
            return self._client

        @nats_client.setter
        def nats_client(self, value: object | None) -> None:
            if value is not None:
                raise assignment_error
            self._client = value

    app = SimpleNamespace(state=State())
    request = SimpleNamespace(app=app)

    async def connect(*_args: object, **_kwargs: object) -> Client:
        return client

    monkeypatch.setattr(deps_module.nats, "connect", connect)
    caught: BaseException | None = None
    try:
        await deps_module.get_jetstream(cast(Any, request))
    except BaseException as error:
        caught = error
    assert caught is assignment_error
    assert close_count == 1
