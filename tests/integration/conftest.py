"""Shared helpers for integration tests (require the running dev compose stack).

Run with: make test-integration  (== uv run pytest -m integration tests/integration)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Literal, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_BASE = ("compose.yaml", "compose.dev.yaml")
ComposeMode = Literal["rootful", "rootless"]

API_URL = os.environ.get("JHIN_API_URL", "http://localhost:8000")
WEB_URL = os.environ.get("JHIN_WEB_URL", "http://localhost:3000")
TEMPORAL_ADDRESS = os.environ.get("JHIN_TEMPORAL_ADDRESS", "localhost:7233")
NATS_URL = os.environ.get("JHIN_NATS_URL", "nats://localhost:4222")
FAKE_GITHUB_URL = os.environ.get("JHIN_FAKE_GITHUB_URL", "http://localhost:8091")
FAKE_LINEAR_URL = os.environ.get("JHIN_FAKE_LINEAR_URL", "http://localhost:8092")
FAKE_VERCEL_URL = os.environ.get("JHIN_FAKE_VERCEL_URL", "http://localhost:8094")
FAKE_SUPABASE_URL = os.environ.get("JHIN_FAKE_SUPABASE_URL", "http://localhost:8095")

POSTGRES_HOST = os.environ.get("JHIN_POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.environ.get("POSTGRES_DEV_PORT", "55432"))
PHASE9_DB_READER_DSN = os.environ.get(
    "JHIN_PHASE9_DB_READER_DSN",
    "postgresql://jhin_reader:reader-pass@127.0.0.1:55433/supabase_fixture",
)
PHASE9_DB_WRITER_DSN = os.environ.get(
    "JHIN_PHASE9_DB_WRITER_DSN",
    "postgresql://jhin_writer:writer-pass@127.0.0.1:55433/supabase_fixture",
)
PHASE9_DB_ADMIN_DSN = os.environ.get(
    "JHIN_PHASE9_DB_ADMIN_DSN",
    "postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:55433/supabase_fixture",
)

_COMPOSE_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def selected_compose_mode(value: str | None = None) -> ComposeMode:
    """Return the explicitly selected socket mode (rootful for legacy unit seams)."""
    selected = value if value is not None else os.environ.get("PHASE10_SOCKET_MODE", "rootful")
    if selected not in {"rootful", "rootless"}:
        raise ValueError("PHASE10_SOCKET_MODE must be exactly rootful or rootless")
    return cast(ComposeMode, selected)


def compose_files_for_mode(mode: ComposeMode) -> tuple[str, ...]:
    """Build the one-authority integration vector for a selected mode."""
    return (*COMPOSE_BASE, f"compose.{mode}.yaml")


def required_services_for_mode(mode: str) -> set[str]:
    """Services whose presence and health gate the selected integration stack."""
    selected = selected_compose_mode(mode)
    required = {
        "api",
        "web",
        "workflow-worker",
        "agent-worker",
        "tool-worker",
        "sandbox-runner",
        "event-worker",
        "postgres",
        "nats",
        "temporal",
    }
    if selected == "rootless":
        required.add("rootless-docker-transport")
    return required


def validate_compose_project(project: str) -> str:
    """Validate an explicit Compose project before it reaches ``-p``."""
    if _COMPOSE_PROJECT_PATTERN.fullmatch(project) is None:
        raise ValueError(
            "JHIN_TEST_COMPOSE_PROJECT must start with a lowercase letter or digit "
            "and contain only lowercase letters, digits, dashes, or underscores"
        )
    return project


def compose(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a docker compose subcommand against the dev stack."""
    project = validate_compose_project(os.environ.get("JHIN_TEST_COMPOSE_PROJECT", "jhin"))
    selected = os.environ.get("PHASE10_SOCKET_MODE")
    files = compose_files_for_mode(selected_compose_mode(selected)) if selected else COMPOSE_BASE
    command = ["docker", "compose", "-p", project]
    for filename in files:
        command.extend(("-f", filename))
    return subprocess.run(
        [*command, *args],
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
    required = required_services_for_mode(selected_compose_mode())
    missing = required - running
    if missing:
        pytest.fail(
            f"integration tests need the dev stack running (make dev); missing: {sorted(missing)}"
        )
