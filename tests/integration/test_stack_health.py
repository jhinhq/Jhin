"""Phase 1 exit test (a): the compose stack is healthy end to end."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.integration.conftest import API_URL, WEB_URL, compose

pytestmark = pytest.mark.integration


def test_all_compose_services_healthy() -> None:
    # Poll: sibling tests restart services, whose healthchecks have a
    # start_period during which they report "starting".
    deadline = time.monotonic() + 90
    while True:
        result = compose("ps", "--format", "{{.Service}} {{.Health}}")
        statuses = dict(
            line.split(maxsplit=1) for line in result.stdout.strip().splitlines() if " " in line
        )
        unhealthy = {s: h for s, h in statuses.items() if h != "healthy"}
        if not unhealthy:
            break
        if time.monotonic() > deadline or any(h == "unhealthy" for h in unhealthy.values()):
            pytest.fail(f"services not healthy: {unhealthy}")
        time.sleep(3)


async def test_api_liveness() -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_api_readiness_reports_all_dependencies_ok() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{API_URL}/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    dependency_status = {dep["name"]: dep["status"] for dep in body["dependencies"]}
    assert dependency_status == {"postgres": "ok", "nats": "ok", "temporal": "ok"}
    assert body["status"] == "ok"


async def test_web_shell_serves() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(WEB_URL)
    assert response.status_code == 200
    assert "Jhin" in response.text
