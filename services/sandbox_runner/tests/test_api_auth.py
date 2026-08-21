"""The runner API fails closed: bad/missing/unconfigured token = 401 on all
job endpoints; only /health is open (it returns no job data)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from fastapi import FastAPI

from jhin_observability import (
    ObservabilityConfig,
    ObservabilityRuntime,
    initialize_observability,
)
from jhin_sandbox_runner.main import create_app
from jhin_sandbox_runner.settings import Settings


@pytest.fixture
def caller_runtime() -> Iterator[ObservabilityRuntime]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_disabled = root.disabled
    original_named = {
        candidate: (list(candidate.handlers), candidate.level, candidate.propagate)
        for candidate in logging.root.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    }
    original_structlog_config = cast(
        dict[str, Any],
        {
            key: list(value) if key == "processors" else value
            for key, value in structlog.get_config().items()
        },
    )
    runtime: ObservabilityRuntime | None = None
    try:
        runtime = initialize_observability(
            ObservabilityConfig(
                service_name="sandbox-auth-test",
                service_version="0.0.0",
                environment="test",
            )
        )
        yield runtime
    finally:
        try:
            if runtime is not None:
                runtime.shutdown(timeout_millis=5_000)
        finally:
            installed_handlers = [
                handler for handler in root.handlers if handler not in original_handlers
            ]
            root.handlers[:] = original_handlers
            root.setLevel(original_level)
            root.disabled = original_disabled
            for named, (handlers, level, propagate) in original_named.items():
                named.handlers[:] = handlers
                named.setLevel(level)
                named.propagate = propagate
            structlog.configure(**original_structlog_config)
            for handler in installed_handlers:
                handler.close()


def app_for(token: str, runtime: ObservabilityRuntime) -> FastAPI:
    return create_app(
        Settings(
            sandbox_runner_token=token,
            sandbox_default_image="jhin-sandbox:test",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
        ),
        runtime=runtime,
    )


def client_for(token: str, runtime: ObservabilityRuntime) -> httpx.AsyncClient:
    app = app_for(token, runtime)
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
async def test_job_endpoints_reject_bad_auth(
    headers: dict[str, str],
    caller_runtime: ObservabilityRuntime,
) -> None:
    async with client_for("correct-token", caller_runtime) as client:
        for method, path in [
            ("POST", "/v1/jobs"),
            ("GET", "/v1/jobs/abc"),
            ("GET", "/v1/jobs/abc/logs"),
            ("POST", "/v1/jobs/abc/cancel"),
            ("DELETE", "/v1/workspaces/abc"),
        ]:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401, (method, path, response.status_code)


async def test_unconfigured_token_denies_everything(
    caller_runtime: ObservabilityRuntime,
) -> None:
    async with client_for("", caller_runtime) as client:
        response = await client.get("/v1/jobs/abc", headers={"Authorization": "Bearer "})
        assert response.status_code == 401


async def test_valid_token_reaches_handler(caller_runtime: ObservabilityRuntime) -> None:
    async with client_for("correct-token", caller_runtime) as client:
        response = await client.get(
            "/v1/jobs/missing", headers={"Authorization": "Bearer correct-token"}
        )
        assert response.status_code == 404  # authorized, job simply absent


@pytest.mark.parametrize(("daemon_ok", "status_code"), [(True, 200), (False, 503)])
async def test_health_is_daemon_backed(
    daemon_ok: bool,
    status_code: int,
    caller_runtime: ObservabilityRuntime,
) -> None:
    app = app_for("correct-token", caller_runtime)
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
