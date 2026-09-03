"""Shared consumers of the isolated Phase 10 Compose authority.

The outer launcher starts a unique stack and publishes Docker-assigned
endpoints before pytest imports test modules. This conftest never guesses a
default project or attaches to an ordinary ``jhin`` deployment.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Literal, cast

import pytest

from .phase10_upgrade_harness import (
    EXPECTED_DESKTOP_SERVICES,
    EXPECTED_ROOTFUL_SERVICES,
    EXPECTED_ROOTLESS_SERVICES,
    ComposeAuthority,
    read_authority_lease,
)
from .phase10_upgrade_harness import (
    run_command as run_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_BASE = ("compose.yaml", "compose.dev.yaml")
ComposeMode = Literal["rootful", "rootless", "desktop"]
_COMPOSE_MODES = ("rootful", "rootless", "desktop")

API_URL = os.environ.get("JHIN_API_URL", "http://localhost:8000")
WEB_URL = os.environ.get("JHIN_WEB_URL", "http://localhost:3000")
TEMPORAL_ADDRESS = os.environ.get("JHIN_TEMPORAL_ADDRESS", "localhost:7233")
NATS_URL = os.environ.get("JHIN_NATS_URL", "nats://localhost:4222")
FAKE_GITHUB_URL = os.environ.get("JHIN_FAKE_GITHUB_URL", "http://localhost:8091")
FAKE_LINEAR_URL = os.environ.get("JHIN_FAKE_LINEAR_URL", "http://localhost:8092")
FAKE_VERCEL_URL = os.environ.get("JHIN_FAKE_VERCEL_URL", "http://localhost:8094")
FAKE_SUPABASE_URL = os.environ.get("JHIN_FAKE_SUPABASE_URL", "http://localhost:8095")

POSTGRES_HOST = os.environ.get("JHIN_POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.environ.get("JHIN_POSTGRES_PORT", "55432"))
PHASE9_DB_READER_DSN = os.environ.get(
    "JHIN_PHASE9_DB_READER_DSN",
    "postgresql://jhin_reader:reader-pass@127.0.0.1:55433/supabase_fixture",
)
PHASE9_DB_ADMIN_DSN = os.environ.get(
    "JHIN_PHASE9_DB_ADMIN_DSN",
    "postgresql://postgres:phase9-fixture-admin-only@127.0.0.1:55433/supabase_fixture",
)

_COMPOSE_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_PHASE10_COUNTS = {"passed": 0, "skipped": 0, "xfailed": 0, "deselected": 0}


def selected_compose_mode(value: str | None = None) -> ComposeMode:
    """Return the explicitly selected socket mode, failing closed when absent."""
    selected = value if value is not None else os.environ.get("PHASE10_SOCKET_MODE")
    if selected is None:
        raise ValueError(
            "PHASE10_SOCKET_MODE is required and must be rootful, rootless, or desktop"
        )
    if selected not in _COMPOSE_MODES:
        raise ValueError("PHASE10_SOCKET_MODE must be exactly rootful, rootless, or desktop")
    return cast(ComposeMode, selected)


def required_services_for_mode(mode: str) -> set[str]:
    """Every non-profile dev service required healthy in the selected mode."""
    selected = selected_compose_mode(mode)
    if selected == "rootless":
        return set(EXPECTED_ROOTLESS_SERVICES)
    if selected == "desktop":
        return set(EXPECTED_DESKTOP_SERVICES)
    return set(EXPECTED_ROOTFUL_SERVICES)


def validate_compose_project(project: str) -> str:
    """Validate an explicit Compose project before it reaches ``-p``."""
    if _COMPOSE_PROJECT_PATTERN.fullmatch(project) is None:
        raise ValueError(
            "JHIN_TEST_COMPOSE_PROJECT must start with a lowercase letter or digit "
            "and contain only lowercase letters, digits, dashes, or underscores"
        )
    return project


def validate_compose_arguments(args: tuple[str, ...]) -> tuple[str, ...]:
    """Permit only the integration operations that cannot replace the lease."""
    blocked_options = {
        "-H",
        "--context",
        "--env-file",
        "--file",
        "--host",
        "--project-directory",
        "--project-name",
        "-f",
        "-p",
    }
    blocked_prefixes = tuple(f"{option}=" for option in blocked_options)
    if any(
        argument in blocked_options or argument.startswith(blocked_prefixes) for argument in args
    ):
        raise ValueError("not an allowed leased Compose operation")

    # Worker restart/stop/start vectors are the only lifecycle operations a
    # leased test may perform: they recycle one named service container in
    # place and cannot republish ports, images, files, or the project.
    allowed = (
        args
        in {
            ("restart", "agent-worker"),
            ("restart", "workflow-worker"),
            ("stop", "event-worker"),
            ("start", "event-worker"),
        }
        or args
        in {
            ("ps",),
            ("ps", "--all"),
            ("ps", "--all", "--format", "json"),
        }
        or args
        in {
            ("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
            ("run", "--rm", "--no-deps", "api", "jhin-seed-dev"),
        }
        or (len(args) >= 4 and args[:4] == ("exec", "-T", "postgres", "psql"))
    )
    if not allowed:
        raise ValueError("not an allowed leased Compose operation")
    return args


def compose_authority() -> ComposeAuthority:
    raw_lease = os.environ.get("JHIN_PHASE10_AUTHORITY_LEASE")
    if not raw_lease:
        raise RuntimeError("a Compose authority lease is required")
    authority = read_authority_lease(Path(raw_lease), expected_repo=REPO_ROOT)
    requested_mode = selected_compose_mode()
    if authority.mode != requested_mode:
        raise RuntimeError("authority lease socket mode differs from PHASE10_SOCKET_MODE")
    if authority.project == "jhin":
        raise RuntimeError("ordinary jhin project is forbidden in integration acceptance")
    return authority


def compose(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run one exact-vector Compose operation through the leased authority."""
    validated = validate_compose_arguments(args)
    authority = compose_authority()
    authority.assert_socket_unchanged()
    result = run_command(
        authority.compose_command(*validated),
        env=authority.environment,
        cwd=authority.repo,
        timeout=timeout,
        check=True,
    )
    return cast(subprocess.CompletedProcess[str], result)


def stack_readiness_required(scenario: str | None) -> bool:
    """The wrong-GID negative owns only a deliberately failing runner."""
    return scenario != "wrong-gid"


@pytest.fixture(scope="session", autouse=True)
def _require_stack(request: pytest.FixtureRequest) -> None:
    """Gate live items once; pure helper nodes never contact Docker."""
    integration_items = [
        item for item in request.session.items if item.get_closest_marker("integration") is not None
    ]
    if not integration_items:
        return
    try:
        authority = compose_authority()
        if stack_readiness_required(os.environ.get("JHIN_PHASE10_SCENARIO")):
            # The harness saw every service healthy before it started this
            # process, but a worker can drop back to "starting" in the gap - a
            # NATS reconnect under rootless Docker is enough - so this is a
            # bounded wait, not a single look.
            authority.wait_ready()
        else:
            authority.assert_socket_unchanged()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        pytest.fail(f"isolated Phase 10 Compose stack is not ready: {error}")


def pytest_configure(config: pytest.Config) -> None:
    del config
    _PHASE10_COUNTS.update(passed=0, skipped=0, xfailed=0, deselected=0)


def pytest_deselected(items: list[pytest.Item]) -> None:
    _PHASE10_COUNTS["deselected"] += len(items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if getattr(report, "wasxfail", False):
        _PHASE10_COUNTS["xfailed"] += 1
    elif report.skipped:
        _PHASE10_COUNTS["skipped"] += 1
    elif report.when == "call" and report.passed:
        _PHASE10_COUNTS["passed"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get("JHIN_PHASE10_STRICT_SELECTION") != "1":
        return
    expected_raw = os.environ.get("JHIN_PHASE10_EXPECTED_TESTS")
    try:
        expected = int(expected_raw or "")
    except ValueError:
        expected = -1
    invalid = (
        expected < 1
        or _PHASE10_COUNTS["passed"] != expected
        or _PHASE10_COUNTS["skipped"] != 0
        or _PHASE10_COUNTS["xfailed"] != 0
        or _PHASE10_COUNTS["deselected"] != 0
    )
    if invalid and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
