"""Shared helpers for integration tests (require the running dev compose stack).

Run with: make test-integration  (== uv run pytest -m integration tests/integration)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.dev.yaml"]

API_URL = "http://localhost:8000"
WEB_URL = "http://localhost:3000"
TEMPORAL_ADDRESS = "localhost:7233"
NATS_URL = "nats://localhost:4222"


def compose(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a docker compose subcommand against the dev stack."""
    return subprocess.run(
        [*COMPOSE, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


@pytest.fixture(scope="session", autouse=True)
def _require_stack() -> None:
    """Fail fast with a clear message when the stack is not up."""
    try:
        result = compose("ps", "--services", "--filter", "status=running")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.fail(f"docker compose stack not reachable: {exc}")
    running = set(result.stdout.split())
    required = {"api", "web", "workflow-worker", "event-worker", "postgres", "nats", "temporal"}
    missing = required - running
    if missing:
        pytest.fail(
            f"integration tests need the dev stack running (make dev); missing: {sorted(missing)}"
        )
