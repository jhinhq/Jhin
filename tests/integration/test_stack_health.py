"""Phase 1 exit test (a): the compose stack is healthy end to end."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    API_URL,
    WEB_URL,
    compose,
    required_services_for_mode,
    selected_compose_mode,
)

pytestmark = pytest.mark.integration


def _compose_ps_rows(output: str) -> list[dict[str, Any]]:
    """Decode Compose v2 array or NDJSON output with unique service identities."""
    if not output.strip():
        raise ValueError("Compose ps output is empty")
    try:
        decoded: object = json.loads(output)
    except json.JSONDecodeError:
        decoded_rows: list[object] = []
        for line_number, line in enumerate(output.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                decoded_rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Compose ps NDJSON row {line_number} is malformed") from exc
        decoded = decoded_rows

    if isinstance(decoded, dict):
        candidates: list[object] = [decoded]
    elif isinstance(decoded, list):
        candidates = decoded
    else:
        raise ValueError("Compose ps output must contain JSON objects")

    rows: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError(f"Compose ps row {index} must be an object")
        raw_identity = candidate.get("Service")
        if not isinstance(raw_identity, str) or not raw_identity.strip():
            raise ValueError(f"Compose ps row {index} has no service identity")
        identity = raw_identity.strip()
        if identity in identities:
            raise ValueError(f"Compose ps contains duplicate service identity: {identity}")
        identities.add(identity)
        rows.append(candidate)
    return rows


def unhealthy_expected_services(output: str, expected: set[str]) -> dict[str, str]:
    """Return every missing, stopped, blank, or non-healthy expected service."""
    rows = _compose_ps_rows(output)
    by_service = {str(row["Service"]).strip(): row for row in rows}
    failures: dict[str, str] = {}
    for service in sorted(expected):
        row: dict[str, Any] | None = by_service.get(service)
        if row is None:
            failures[service] = "missing"
            continue
        state = str(row.get("State", "")).strip()
        health = str(row.get("Health", "")).strip()
        if state != "running" or health != "healthy":
            failures[service] = f"state={state or '<blank>'} health={health or '<blank>'}"
    return failures


def test_all_compose_services_healthy() -> None:
    # Poll: sibling tests restart services, whose healthchecks have a
    # start_period during which they report "starting".
    deadline = time.monotonic() + 90
    expected = required_services_for_mode(selected_compose_mode())
    while True:
        result = compose("ps", "--all", "--format", "json")
        unhealthy = unhealthy_expected_services(result.stdout, expected)
        if not unhealthy:
            break
        if time.monotonic() > deadline or any(
            "health=unhealthy" in status or "state=exited" in status
            for status in unhealthy.values()
        ):
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
