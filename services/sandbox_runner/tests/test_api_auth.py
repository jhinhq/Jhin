"""The runner API fails closed: bad/missing/unconfigured token = 401 on all
job endpoints; only /health is open (it returns no job data)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from jhin_sandbox_runner.main import create_app
from jhin_sandbox_runner.settings import Settings


def app_for(token: str) -> FastAPI:
    return create_app(
        Settings(
            sandbox_runner_token=token,
            sandbox_default_image="jhin-sandbox:test",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
        )
    )


def client_for(token: str) -> httpx.AsyncClient:
    app = app_for(token)
    # No lifespan: auth is checked before any manager/Docker interaction.
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://runner")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic abc"},
    ],
)
async def test_job_endpoints_reject_bad_auth(headers: dict[str, str]) -> None:
    async with client_for("correct-token") as client:
        for method, path in [
            ("POST", "/v1/jobs"),
            ("GET", "/v1/jobs/abc"),
            ("GET", "/v1/jobs/abc/logs"),
            ("POST", "/v1/jobs/abc/cancel"),
            ("DELETE", "/v1/workspaces/abc"),
        ]:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401, (method, path, response.status_code)


async def test_unconfigured_token_denies_everything() -> None:
    async with client_for("") as client:
        response = await client.get("/v1/jobs/abc", headers={"Authorization": "Bearer "})
        assert response.status_code == 401


async def test_valid_token_reaches_handler() -> None:
    async with client_for("correct-token") as client:
        response = await client.get(
            "/v1/jobs/missing", headers={"Authorization": "Bearer correct-token"}
        )
        assert response.status_code == 404  # authorized, job simply absent


@pytest.mark.parametrize(("daemon_ok", "status_code"), [(True, 200), (False, 503)])
async def test_health_is_daemon_backed(daemon_ok: bool, status_code: int) -> None:
    app = app_for("correct-token")
    ping = AsyncMock(return_value=daemon_ok)
    app.state.manager.ping = ping
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        response = await client.get("/health")
    assert response.status_code == status_code
    assert response.json() == {
        "status": "ok" if daemon_ok else "unavailable",
        "docker": daemon_ok,
    }
