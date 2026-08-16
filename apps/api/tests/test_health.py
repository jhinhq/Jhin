from fastapi.testclient import TestClient

from jhin_api.health import service
from jhin_api.main import create_app


def test_liveness_reports_ok_and_app_name() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "Jhin"


def test_readiness_ok_when_all_dependencies_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def ok(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service, "check_postgres", ok)
    monkeypatch.setattr(service, "check_nats", ok)
    monkeypatch.setattr(service, "check_temporal", ok)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {dep["name"] for dep in body["dependencies"]} == {"postgres", "nats", "temporal"}
    assert all(dep["status"] == "ok" for dep in body["dependencies"])


def test_readiness_degraded_returns_503(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def ok(*args: object, **kwargs: object) -> None:
        return None

    async def boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("unreachable")

    monkeypatch.setattr(service, "check_postgres", ok)
    monkeypatch.setattr(service, "check_nats", boom)
    monkeypatch.setattr(service, "check_temporal", ok)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    nats_dep = next(dep for dep in body["dependencies"] if dep["name"] == "nats")
    assert nats_dep["status"] == "error"
    assert "unreachable" in nats_dep["detail"]
