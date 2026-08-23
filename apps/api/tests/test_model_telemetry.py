"""Real-lifespan API ownership tests for model verification telemetry."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import traceback
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

import jhin_models.factory as model_factory
from jhin_api.deps import AuthContext, WorkspaceContext, get_current_auth, get_secret_crypto
from jhin_api.main import create_app
from jhin_api.models import service as model_service
from jhin_api.models.router import providers_router
from jhin_api.settings import Settings
from jhin_db.base import Base
from jhin_db.models import (
    AuditEvent,
    ModelProvider,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import ModelProviderType, WorkspaceRole, new_uuid7
from jhin_models.base import ModelClient, ModelProviderError, ModelRequest, ModelResponse
from jhin_models.factory import ProviderConfigError
from jhin_observability import (
    JhinMetrics,
    ObservabilityNotInitializedError,
    get_runtime,
    noop_metrics,
    noop_tracer,
)
from jhin_observability.metrics import build_jhin_metrics
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.crypto import (
    MasterKey,
    decode_master_key_material,
    generate_master_key_material,
)


class _VerificationClient(ModelClient):
    def __init__(
        self,
        detail: object = "provider verified",
        *,
        verify_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.detail = detail
        self.verify_error = verify_error
        self.close_error = close_error
        self.verify_calls = 0
        self.close_calls = 0
        self.events: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("verification must not generate")

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise AssertionError("verification must not stream")

    async def verify(self) -> str:
        self.events.append("verify")
        self.verify_calls += 1
        if self.verify_error is not None:
            raise self.verify_error
        return self.detail  # type: ignore[return-value]

    async def close(self) -> None:
        self.events.append("close")
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@dataclass
class _RecordingTelemetry:
    metrics: JhinMetrics
    reader: InMemoryMetricReader
    metric_provider: MeterProvider
    tracer: Tracer
    exporter: InMemorySpanExporter
    trace_provider: TracerProvider


@pytest.fixture
def recording_telemetry() -> Iterator[_RecordingTelemetry]:
    reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    metrics = build_jhin_metrics(cast(Meter, metric_provider.get_meter("api-model-test", "1")))
    exporter = InMemorySpanExporter()
    trace_provider = TracerProvider(
        resource=Resource.create({"service.name": "api-model-test", "safe.resource": "bounded"}),
        shutdown_on_exit=False,
    )
    trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = trace_provider.get_tracer("api-model-test", "1")
    owned = _RecordingTelemetry(
        metrics,
        reader,
        metric_provider,
        tracer,
        exporter,
        trace_provider,
    )
    try:
        yield owned
    finally:
        trace_provider.shutdown()
        metric_provider.shutdown()


def _complete_recording_payload(telemetry: _RecordingTelemetry) -> str:
    spans: list[dict[str, Any]] = []
    for span in telemetry.exporter.get_finished_spans():
        spans.append(
            {
                "name": span.name,
                "kind": span.kind.name,
                "attributes": dict(span.attributes or {}),
                "status": {
                    "code": span.status.status_code.name,
                    "description": span.status.description,
                },
                "events": [
                    {
                        "name": event.name,
                        "attributes": dict(event.attributes or {}),
                    }
                    for event in span.events
                ],
                "links": [
                    {
                        "attributes": dict(link.attributes or {}),
                        "trace_state": list(link.context.trace_state.items()),
                    }
                    for link in span.links
                ],
                "context_trace_state": (
                    [] if span.context is None else list(span.context.trace_state.items())
                ),
                "parent_trace_state": (
                    [] if span.parent is None else list(span.parent.trace_state.items())
                ),
                "resource": dict(span.resource.attributes),
                "resource_schema_url": span.resource.schema_url,
                "scope": {
                    "name": span.instrumentation_scope.name,
                    "version": span.instrumentation_scope.version,
                    "schema_url": span.instrumentation_scope.schema_url,
                    "attributes": dict(span.instrumentation_scope.attributes or {}),
                },
            }
        )
    metrics: list[dict[str, Any]] = []
    data = telemetry.reader.get_metrics_data()
    if data is not None:
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    metrics.append(
                        {
                            "name": metric.name,
                            "description": metric.description,
                            "unit": metric.unit,
                            "resource": dict(resource_metrics.resource.attributes),
                            "resource_schema_url": resource_metrics.schema_url,
                            "scope": {
                                "name": scope_metrics.scope.name,
                                "version": scope_metrics.scope.version,
                                "schema_url": scope_metrics.scope.schema_url,
                                "attributes": dict(scope_metrics.scope.attributes or {}),
                                "metrics_schema_url": scope_metrics.schema_url,
                            },
                            "points": [
                                {
                                    "attributes": dict(point.attributes or {}),
                                    "value": getattr(point, "value", None),
                                    "sum": getattr(point, "sum", None),
                                    "count": getattr(point, "count", None),
                                    "exemplars": [
                                        {
                                            "attributes": dict(exemplar.filtered_attributes or {}),
                                            "value": exemplar.value,
                                            "trace_id": exemplar.trace_id,
                                            "span_id": exemplar.span_id,
                                        }
                                        for exemplar in (getattr(point, "exemplars", ()) or ())
                                    ],
                                }
                                for point in metric.data.data_points
                            ],
                        }
                    )
    return json.dumps({"spans": spans, "metrics": metrics}, sort_keys=True, default=str)


def _crypto() -> SecretCrypto:
    material = generate_master_key_material()
    return SecretCrypto(MasterKey(key=decode_master_key_material(material)))


def _test_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite://",
        otel_exporter_otlp_endpoint=None,
        otel_traces_sampler="always_on",
    )


async def _seed_provider(app: FastAPI) -> tuple[User, Workspace, ModelProvider]:
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with app.state.session_factory() as session:
        user = User(
            email=f"model-owner-{new_uuid7().hex[:8]}@example.com",
            display_name="Model owner",
            password_hash="x",
        )
        workspace = Workspace(name="Model API", slug=f"model-api-{new_uuid7().hex[:8]}")
        session.add_all((user, workspace))
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WorkspaceRole.ADMIN.value,
            )
        )
        provider = ModelProvider(
            workspace_id=workspace.id,
            type=ModelProviderType.OLLAMA.value,
            display_name="Private provider display name",
            base_url="http://localhost:11434/v1",
        )
        session.add(provider)
        await session.commit()
        return user, workspace, provider


async def _seed_unit_provider(session: AsyncSession, ctx: WorkspaceContext) -> ModelProvider:
    provider = ModelProvider(
        workspace_id=ctx.workspace_id,
        type=ModelProviderType.OLLAMA.value,
        display_name="Unit verification provider",
        base_url="http://localhost:11434/v1",
    )
    session.add(provider)
    await session.commit()
    return provider


async def _durable_verification_state(
    session: AsyncSession, provider_id: object
) -> tuple[ModelProvider, list[AuditEvent]]:
    async with AsyncSession(bind=session.bind) as durable:
        provider = await durable.get(ModelProvider, provider_id)
        assert provider is not None
        audits = list(
            await durable.scalars(select(AuditEvent).where(AuditEvent.target_id == provider.id))
        )
        return provider, audits


async def test_real_lifespan_provider_verification_uses_exact_app_owned_handles_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_test_settings())
    observed: list[tuple[object, object, object, object, object]] = []
    fake = _VerificationClient()
    owned_runtime: object | None = None

    def fake_build_model_client(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        observed.append((provider_type, base_url, api_key, metrics, tracer))
        assert kwargs == {"admin_api_key": None}
        return fake

    monkeypatch.setattr(model_service, "build_model_client", fake_build_model_client)
    async with app.router.lifespan_context(app):
        owned_runtime = app.state.observability
        user, workspace, provider = await _seed_provider(app)
        auth = AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"model-route-{new_uuid7()}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

        async def override_auth() -> AuthContext:
            return auth

        async def override_crypto() -> SecretCrypto:
            return _crypto()

        app.dependency_overrides[get_current_auth] = override_auth
        app.dependency_overrides[get_secret_crypto] = override_crypto
        assert set(app.dependency_overrides) == {get_current_auth, get_secret_crypto}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.cookies.set("jhin_csrf", "model-route-csrf")
            response = await client.post(
                f"/api/v1/workspaces/{workspace.id}/model-providers/{provider.id}/verify",
                headers={"x-csrf-token": "model-route-csrf"},
            )

        assert response.status_code == 200
        assert response.json() == {"ok": True, "detail": "provider verified"}
        assert observed == [
            (
                ModelProviderType.OLLAMA.value,
                "http://localhost:11434/v1",
                None,
                app.state.observability.metrics,
                app.state.observability.tracer,
            )
        ]
        assert fake.verify_calls == 1
        assert fake.close_calls == 1
        async with app.state.session_factory() as session:
            durable_provider = await session.get(ModelProvider, provider.id)
            assert durable_provider is not None
            assert durable_provider.last_verified_at is not None
            assert durable_provider.last_error is None
            audits = list(
                await session.scalars(select(AuditEvent).where(AuditEvent.target_id == provider.id))
            )
            assert [audit.action for audit in audits] == ["provider.verified"]
            assert audits[0].metadata_json == {
                "display_name": "Private provider display name",
                "ok": True,
            }
        assert get_runtime() is owned_runtime

    assert owned_runtime is not None
    assert app.state.observability is owned_runtime
    assert owned_runtime._shutdown_state == "complete"
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()


@pytest.mark.asyncio
async def test_configuration_failure_preserves_detail_and_commits_failed_audit(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    provider = await _seed_unit_provider(session, admin_ctx)
    provider_id = provider.id
    metrics = noop_metrics()
    tracer = noop_tracer()
    failure = ProviderConfigError("provider configuration unavailable")
    observed: list[tuple[object, object, object, object, object]] = []

    def fail_build(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        observed.append((provider_type, base_url, api_key, metrics, tracer))
        assert kwargs == {"admin_api_key": None}
        raise failure

    monkeypatch.setattr(model_service, "build_model_client", fail_build)

    result = await model_service.verify_provider(
        session,
        crypto,
        admin_ctx,
        provider_id,
        metrics,
        tracer,
        request_id=new_uuid7(),
        ip_hash="configuration-failure-ip-hash",
    )

    assert result == (False, "provider configuration unavailable")
    assert observed == [
        (
            ModelProviderType.OLLAMA.value,
            "http://localhost:11434/v1",
            None,
            metrics,
            tracer,
        )
    ]
    durable_provider, audits = await _durable_verification_state(session, provider_id)
    assert durable_provider.last_verified_at is None
    assert durable_provider.last_error == "provider configuration unavailable"
    assert [audit.action for audit in audits] == ["provider.verify_failed"]
    assert audits[0].metadata_json == {
        "display_name": "Unit verification provider",
        "ok": False,
    }


@pytest.mark.asyncio
async def test_provider_failure_is_redacted_once_closed_and_committed_with_exact_handles(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    provider = await _seed_unit_provider(session, admin_ctx)
    provider_id = provider.id
    metrics = noop_metrics()
    tracer = noop_tracer()
    failure = ModelProviderError("provider-error-authority-canary")
    fake = _VerificationClient(verify_error=failure)
    observed: list[tuple[object, object]] = []
    redacted: list[str] = []

    def fake_build_model_client(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        assert provider_type == ModelProviderType.OLLAMA.value
        assert base_url == "http://localhost:11434/v1"
        assert api_key is None
        assert kwargs == {"admin_api_key": None}
        observed.append((metrics, tracer))
        return fake

    def fake_redact_text(detail: str) -> str:
        redacted.append(detail)
        return "redacted-provider-error"

    monkeypatch.setattr(model_service, "build_model_client", fake_build_model_client)
    monkeypatch.setattr(model_service, "redact_text", fake_redact_text)

    result = await model_service.verify_provider(
        session,
        crypto,
        admin_ctx,
        provider_id,
        metrics,
        tracer,
        request_id=new_uuid7(),
        ip_hash="provider-failure-ip-hash",
    )

    assert result == (False, "redacted-provider-error")
    assert observed == [(metrics, tracer)]
    assert redacted == ["provider-error-authority-canary"]
    assert fake.events == ["verify", "close"]
    assert fake.verify_calls == 1
    assert fake.close_calls == 1
    durable_provider, audits = await _durable_verification_state(session, provider_id)
    assert durable_provider.last_verified_at is None
    assert durable_provider.last_error == "redacted-provider-error"
    assert [audit.action for audit in audits] == ["provider.verify_failed"]
    assert audits[0].metadata_json == {
        "display_name": "Unit verification provider",
        "ok": False,
    }


@pytest.mark.asyncio
async def test_real_provider_secret_and_metadata_sources_never_reach_recording_sinks(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    recording_telemetry: _RecordingTelemetry,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_id = UUID("018f4d52-8b93-7d41-8ac7-7f190f09cafe")
    base_url = "https://actual-base-url-canary.example/v1"
    api_key = "actual-api-key-canary-material"
    display_name = "actual-provider-display-metadata-canary"
    secret = await SecretStore(session, crypto).create(
        workspace_id=admin_ctx.workspace_id,
        name="actual-provider-secret-source",
        plaintext=api_key,
    )
    provider = ModelProvider(
        id=provider_id,
        workspace_id=admin_ctx.workspace_id,
        type=ModelProviderType.OPENAI_COMPATIBLE.value,
        display_name=display_name,
        base_url=base_url,
        secret_id=secret.id,
    )
    session.add(provider)
    await session.commit()
    raw = _VerificationClient(detail="bounded verification detail")
    adapter_calls: list[tuple[object, object, object]] = []

    def build_raw_adapter(
        *,
        base_url: object,
        api_key: object,
        transport: object,
    ) -> ModelClient:
        adapter_calls.append((base_url, api_key, transport))
        return raw

    monkeypatch.setattr(model_factory, "OpenAICompatibleClient", build_raw_adapter)
    caplog.set_level(logging.WARNING)
    for logger_name in ("jhin_api", "jhin_models", "jhin_secrets"):
        caplog.set_level(logging.DEBUG, logger=logger_name)
    caplog.clear()
    logging.getLogger(model_service.__name__).debug(
        "bounded-provider-privacy-probe",
        extra={"bounded_structured_field": "bounded-structured-value"},
    )
    try:
        result = await model_service.verify_provider(
            session,
            crypto,
            admin_ctx,
            provider_id,
            recording_telemetry.metrics,
            recording_telemetry.tracer,
            request_id=new_uuid7(),
            ip_hash="bounded-provider-source-ip-hash",
        )

        assert result == (True, "bounded verification detail")
        assert adapter_calls == [(base_url, api_key, None)]
        assert raw.verify_calls == raw.close_calls == 1
        model_spans = [
            span
            for span in recording_telemetry.exporter.get_finished_spans()
            if span.name == "model.request"
        ]
        assert len(model_spans) == 1
        assert model_spans[0].attributes["jhin.provider_type"] == "openai_compatible"
        durable_provider, audits = await _durable_verification_state(session, provider_id)
        assert durable_provider.last_verified_at is not None
        assert [audit.action for audit in audits] == ["provider.verified"]
        assert audits[0].metadata_json == {"display_name": display_name, "ok": True}
        captured = capsys.readouterr()
        structured_records = json.dumps(
            [record.__dict__ for record in caplog.records],
            sort_keys=True,
            default=str,
        )
        assert any(
            record.__dict__.get("bounded_structured_field") == "bounded-structured-value"
            for record in caplog.records
        )
        payload = "\n".join(
            (
                _complete_recording_payload(recording_telemetry),
                caplog.text,
                structured_records,
                captured.out,
                captured.err,
            )
        )
        for canary in {str(provider_id), base_url, api_key, display_name}:
            assert canary not in payload
    finally:
        from jhin_secrets import get_redactor

        get_redactor().clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails_first", [False, True])
async def test_close_failure_is_exact_terminal_authority_without_state_or_audit_commit(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    provider_fails_first: bool,
) -> None:
    provider = await _seed_unit_provider(session, admin_ctx)
    provider_id = provider.id
    metrics = noop_metrics()
    tracer = noop_tracer()
    provider_failure = (
        ModelProviderError("provider-failure-before-close") if provider_fails_first else None
    )
    close_failure = RuntimeError("close-failure-authority")
    fake = _VerificationClient(
        verify_error=provider_failure,
        close_error=close_failure,
    )
    observed: list[tuple[object, object]] = []

    def fake_build_model_client(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        assert provider_type == ModelProviderType.OLLAMA.value
        assert base_url == "http://localhost:11434/v1"
        assert api_key is None
        assert kwargs == {"admin_api_key": None}
        observed.append((metrics, tracer))
        return fake

    monkeypatch.setattr(model_service, "build_model_client", fake_build_model_client)

    with pytest.raises(RuntimeError) as caught:
        await model_service.verify_provider(
            session,
            crypto,
            admin_ctx,
            provider_id,
            metrics,
            tracer,
            request_id=new_uuid7(),
            ip_hash="close-failure-ip-hash",
        )

    assert caught.value is close_failure
    assert traceback.extract_tb(caught.value.__traceback__)[-1].name == "close"
    assert observed == [(metrics, tracer)]
    assert fake.events == ["verify", "close"]
    assert fake.verify_calls == 1
    assert fake.close_calls == 1
    await session.rollback()
    durable_provider, audits = await _durable_verification_state(session, provider_id)
    assert durable_provider.last_verified_at is None
    assert durable_provider.last_error is None
    assert audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["verify", "close"])
async def test_verification_cancellation_preserves_first_object_traceback_and_no_commit(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    cancel_phase: str,
) -> None:
    provider = await _seed_unit_provider(session, admin_ctx)
    provider_id = provider.id
    metrics = noop_metrics()
    tracer = noop_tracer()
    cancellation = asyncio.CancelledError(f"{cancel_phase}-cancellation-authority")
    fake = _VerificationClient(
        verify_error=cancellation if cancel_phase == "verify" else None,
        close_error=cancellation if cancel_phase == "close" else None,
    )
    observed: list[tuple[object, object]] = []

    def fake_build_model_client(
        provider_type: object,
        *,
        base_url: object,
        api_key: object,
        metrics: object,
        tracer: object,
        **kwargs: object,
    ) -> ModelClient:
        assert provider_type == ModelProviderType.OLLAMA.value
        assert base_url == "http://localhost:11434/v1"
        assert api_key is None
        assert kwargs == {"admin_api_key": None}
        observed.append((metrics, tracer))
        return fake

    monkeypatch.setattr(model_service, "build_model_client", fake_build_model_client)

    with pytest.raises(asyncio.CancelledError) as caught:
        await model_service.verify_provider(
            session,
            crypto,
            admin_ctx,
            provider_id,
            metrics,
            tracer,
            request_id=new_uuid7(),
            ip_hash="cancellation-ip-hash",
        )

    assert caught.value is cancellation
    assert traceback.extract_tb(caught.value.__traceback__)[-1].name == cancel_phase
    assert observed == [(metrics, tracer)]
    assert fake.events == ["verify", "close"]
    assert fake.verify_calls == 1
    assert fake.close_calls == 1
    await session.rollback()
    durable_provider, audits = await _durable_verification_state(session, provider_id)
    assert durable_provider.last_verified_at is None
    assert durable_provider.last_error is None
    assert audits == []


def test_observability_dependency_validates_exact_app_state_without_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = importlib.import_module("jhin_api.deps")
    dependency = getattr(deps, "get_observability_runtime", None)
    assert dependency is not None, "API must expose its exact app-state runtime dependency"
    bootstrap = importlib.import_module("jhin_observability.bootstrap")

    def forbidden_global() -> None:
        raise AssertionError("API dependency must not read the global runtime")

    monkeypatch.setattr(bootstrap, "get_runtime", forbidden_global)
    app = FastAPI()
    app.state.observability = object()
    request = Request({"type": "http", "app": app, "headers": []})
    with pytest.raises(RuntimeError, match="observability runtime is unavailable"):
        dependency(request)


def test_api_verification_signatures_thread_runtime_handles_in_exact_positions() -> None:
    deps = importlib.import_module("jhin_api.deps")
    assert getattr(deps, "ObservabilityRuntimeDep", None) is not None

    service_signature = inspect.signature(model_service.verify_provider)
    assert list(service_signature.parameters) == [
        "db",
        "crypto",
        "ctx",
        "provider_id",
        "metrics",
        "tracer",
        "request_id",
        "ip_hash",
    ]
    assert service_signature.parameters["metrics"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert service_signature.parameters["tracer"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert service_signature.parameters["request_id"].kind is inspect.Parameter.KEYWORD_ONLY

    route = next(route for route in providers_router.routes if route.name == "verify_provider")
    assert route.endpoint.__name__ == "verify_provider"
    route_signature = inspect.signature(route.endpoint)
    assert "runtime" in route_signature.parameters
