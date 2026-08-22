import asyncio

import httpx
import pytest
from temporalio.exceptions import CancelledError as TemporalCancelledError

import jhin_api.main as main_module
from jhin_api.health import service
from jhin_api.main import create_app
from jhin_api.settings import Settings
from jhin_observability import ObservabilityNotInitializedError, get_runtime


def _test_settings() -> Settings:
    return Settings(
        app_env="test",
        app_name="Jhin",
        app_url="http://test",
        log_level="INFO",
        database_url="sqlite+aiosqlite:///:memory:",
        nats_url="nats://127.0.0.1:4222",
        temporal_address="127.0.0.1:7233",
        otel_exporter_otlp_endpoint=None,
        otel_exporter_otlp_insecure=False,
        otel_traces_sampler="always_on",
    )


@pytest.fixture(autouse=True)
def _deterministic_secret_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_load_secret_crypto", lambda: None)


@pytest.mark.asyncio
async def test_liveness_reports_ok_and_app_name() -> None:
    app = create_app(_test_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health")
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "Jhin"


@pytest.mark.asyncio
async def test_readiness_ok_when_all_dependencies_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service, "check_postgres", ok)
    monkeypatch.setattr(service, "check_nats", ok)
    monkeypatch.setattr(service, "check_temporal", ok)

    app = create_app(_test_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health/ready")
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {dep["name"] for dep in body["dependencies"]} == {"postgres", "nats", "temporal"}
    assert all(dep["status"] == "ok" for dep in body["dependencies"])


@pytest.mark.asyncio
async def test_readiness_degraded_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok(*args: object, **kwargs: object) -> None:
        return None

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("unreachable")

    monkeypatch.setattr(service, "check_postgres", ok)
    monkeypatch.setattr(service, "check_nats", boom)
    monkeypatch.setattr(service, "check_temporal", ok)

    app = create_app(_test_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/health/ready")
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    nats_dep = next(dep for dep in body["dependencies"] if dep["name"] == "nats")
    assert nats_dep["status"] == "error"
    assert "unreachable" in nats_dep["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancellation",
    [asyncio.CancelledError(), TemporalCancelledError()],
)
async def test_timed_preserves_both_cancellation_classes(
    cancellation: BaseException,
) -> None:
    async def cancel() -> None:
        raise cancellation

    caught: BaseException | None = None
    try:
        await service._timed("temporal", cancel)
    except BaseException as exc:
        caught = exc
    assert caught is cancellation


@pytest.mark.asyncio
async def test_readiness_temporal_cancellation_escapes_without_degraded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = TemporalCancelledError()

    async def ok(*_args: object, **_kwargs: object) -> None:
        return None

    async def cancel(*_args: object, **_kwargs: object) -> None:
        raise cancellation

    monkeypatch.setattr(service, "check_postgres", ok)
    monkeypatch.setattr(service, "check_nats", ok)
    monkeypatch.setattr(service, "check_temporal", cancel)
    provider = object()
    caught: BaseException | None = None
    try:
        await service.readiness(_test_settings(), object(), provider)  # type: ignore[arg-type]
    except BaseException as exc:
        caught = exc
    assert caught is cancellation


@pytest.mark.asyncio
async def test_temporal_health_failure_uses_one_closed_private_detail() -> None:
    class Provider:
        async def get(self) -> object:
            raise RuntimeError("temporal-address-rpc-private-canary")

    with pytest.raises(service.TemporalHealthUnavailable) as raised:
        await service.check_temporal(Provider())  # type: ignore[arg-type]
    rendered = str(raised.value)
    assert rendered == "Temporal workflow service is unavailable"
    assert "private-canary" not in rendered
