"""Isolated Docker/Compose authority for Phase 10 live acceptance.

This module is deliberately importable without Docker.  Pure contract tests
exercise parsing and command construction; the CLI at the bottom is the only
entry point permitted to mutate a live daemon for Phase 10 acceptance.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

ComposeMode = Literal["rootful", "rootless"]
UpgradeStage = Literal["parked-phase9", "base-only", "current-phase10"]

EXPECTED_ROOTFUL_SERVICES = {
    "agent-worker",
    "api",
    "event-worker",
    "fake-github",
    "fake-linear",
    "fake-provider",
    "fake-supabase",
    "fake-supabase-db",
    "fake-vercel",
    "nats",
    "postgres",
    "sandbox-runner",
    "temporal",
    "temporal-ui",
    "tool-worker",
    "web",
    "workflow-worker",
}
EXPECTED_ROOTLESS_SERVICES = EXPECTED_ROOTFUL_SERVICES | {"rootless-docker-transport"}

# (Compose interpolation variable, service, container port).  Docker owns all
# host-port allocation; no user-space reserve/close race is permitted.
PUBLISHED_ENDPOINTS = (
    ("WEB_PORT", "web", 3000),
    ("API_PORT", "api", 8000),
    ("FAKE_PROVIDER_DEV_PORT", "fake-provider", 8080),
    ("FAKE_GITHUB_DEV_PORT", "fake-github", 8080),
    ("FAKE_LINEAR_DEV_PORT", "fake-linear", 8080),
    ("FAKE_VERCEL_DEV_PORT", "fake-vercel", 8080),
    ("FAKE_SUPABASE_DEV_PORT", "fake-supabase", 8080),
    ("FAKE_SUPABASE_DB_DEV_PORT", "fake-supabase-db", 5432),
    ("SANDBOX_RUNNER_DEV_PORT", "sandbox-runner", 8085),
    ("POSTGRES_DEV_PORT", "postgres", 5432),
    ("NATS_DEV_PORT", "nats", 4222),
    ("NATS_MONITOR_DEV_PORT", "nats", 8222),
    ("TEMPORAL_DEV_PORT", "temporal", 7233),
    ("TEMPORAL_UI_DEV_PORT", "temporal-ui", 8080),
)

_MODE_ERROR = "mode must be exactly rootful or rootless"
_PERSISTENT_COMPOSE_TIMEOUT_SECONDS = 1200.0
_TARGET_ENVIRONMENT = {
    "APP_ENV",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_REMOVE_ORPHANS",
    "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_ENV_FILES",
    "COMPOSE_DISABLE_ENV_FILE",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
    "DOCKER_DEFAULT_PLATFORM",
    "BUILDX_BUILDER",
    "BUILDKIT_HOST",
}
_TARGET_PREFIXES = ("PHASE10_", "SANDBOX_", "JHIN_TEST_CRASH_BARRIER_")
_PASSTHROUGH_ENVIRONMENT = {
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "DOCKER_CONFIG",
}
_HEX_REF = re.compile(r"[0-9a-f]{40}\Z")
_DOCKER_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{8,16}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_WORKSPACE_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}\Z")
_UPGRADE_SCENARIOS = ("normal", "approval", "sync", "cleanup")
_UPGRADE_FAILPOINTS = {
    "normal": "phase9.agent.after_manifest.before_effect.v1",
    "approval": "phase10.upgrade.approval.placeholder.v1",
    "sync": "phase9.agent.sync.before_effect.v1",
    "cleanup": "phase9.agent.cleanup.before_effect.v1",
}
_BASE_COMPOSE_AUTO_IMAGE_SERVICES = (
    "web",
    "api",
    "workflow-worker",
    "agent-worker",
    "tool-worker",
    "event-worker",
    "fake-provider",
    "fake-github",
    "fake-linear",
    "fake-vercel",
    "fake-supabase",
)
_UPGRADE_COMPOSE_AUTO_IMAGE_SERVICES = tuple(
    f"phase10-{kind}-worker-{scenario}"
    for kind in ("agent", "tool")
    for scenario in _UPGRADE_SCENARIOS
)
_CHILD_BARRIER_JOURNAL_NAME = "phase10-child-barriers.jsonl"
_CHILD_BARRIER_JOURNAL_ENV = "JHIN_PHASE10_CHILD_BARRIER_JOURNAL"


class ComposePsError(ValueError):
    """The selected project is not the exact healthy service topology."""


class ComposeStartingError(ComposePsError):
    """The exact project topology is present but one service is still starting."""


class _LifecycleSignal(BaseException):
    """Catchable process signal latched until child reaping and final cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"Phase 10 lifecycle interrupted by signal {signum}")
        self.signum = signum


class _OwnedProcessGroupSurvived(RuntimeError):
    """The isolated live-child process group survived bounded termination."""

    def __init__(self, process_group: int) -> None:
        super().__init__(f"owned live-child process group {process_group} survived SIGKILL")
        self.process_group = process_group


CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]

_COUNTING_PROVIDER_SCRIPT = r"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jhin_models.testing.fake_openai import DEFAULT_MODELS, build_completion

marker = sys.argv[1]
count = 0
advertised = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        global count
        path = self.path.rstrip("/")
        if path.endswith("/models"):
            self.send_json(200, {"object": "list", "data": [
                {"id": model, "object": "model"} for model in DEFAULT_MODELS
            ]})
        elif path == "/__phase10_health":
            self.send_json(200, {"ok": True})
        elif path == "/__phase10_count":
            with lock:
                observed = count
            self.send_json(200, {"count": observed})
        elif path == "/__phase10_tools":
            with lock:
                observed = list(advertised)
            self.send_json(200, {"tools": observed})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        global advertised, count
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_json(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": {"message": "invalid JSON"}})
            return
        raw_messages = body.get("messages", [])
        messages = json.dumps(raw_messages, separators=(",", ":"))
        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in raw_messages
        )
        if marker in messages and not has_tool_result:
            names = []
            for tool in body.get("tools", []):
                function = tool.get("function", {}) if isinstance(tool, dict) else {}
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    names.append(name)
            with lock:
                count += 1
                advertised = names
        status, payload = build_completion(body)
        self.send_json(status, payload)

    def log_message(self, format, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
""".strip()

_PHASE9_SNAPSHOT_WORKER_SCRIPT = r"""
import asyncio
import sys

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.resources import Resources
from jhin_agent_worker.settings import Settings
from jhin_agent_worker.trigger_activities import TriggerActivities
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput, AgentTaskWorkflow, SnapshotResult
from jhin_workflows.triggered_task import TriggeredTaskWorkflow


async def main():
    include_trigger = sys.argv[1] == "trigger"
    settings = Settings()
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    resources = await Resources.create(settings)
    agents = AgentActivities(resources, temporal_client=client)
    triggers = TriggerActivities(resources)
    finished = asyncio.Event()

    @activity.defn(name="resolve_snapshot")
    async def resolve_snapshot(params: AgentTaskInput) -> SnapshotResult:
        result = await agents.resolve_snapshot_activity(params)
        finished.set()
        return result

    activities = [resolve_snapshot]
    workflows = [AgentTaskWorkflow]
    if include_trigger:
        activities.append(triggers.prepare_triggered_task_activity)
        workflows.append(TriggeredTaskWorkflow)
    try:
        async with Worker(
            client,
            task_queue=AGENT_TASK_QUEUE,
            workflows=workflows,
            activities=activities,
        ):
            await asyncio.wait_for(finished.wait(), timeout=180)
    finally:
        await resources.close()


asyncio.run(main())
""".strip()


@dataclass(frozen=True)
class LiveScenario:
    """One exact live pytest selection owned by the outer lifecycle."""

    nodes: tuple[str, ...]
    expected_tests: int
    upgrade: bool = False
    start_stack: bool = True


@dataclass(frozen=True)
class SandboxArtifact:
    """One job/run pair recorded by this invocation's isolated database."""

    job_id: str
    run_id: str


@dataclass(frozen=True)
class RunningSandboxJob:
    """Exact running job/container pair retained for live isolation inspection."""

    job_id: str
    container_id: str
    container: dict[str, Any]


@dataclass(frozen=True)
class FrozenPhase9Image:
    """Verified immutable image built only from the committed Phase 9 archive."""

    source_ref: str
    tag: str
    image_id: str


@dataclass(frozen=True)
class CountingProvider:
    """Owned nonlogging model-call ledger on the isolated data network."""

    authority: ComposeAuthority
    container_id: str
    name: str
    marker: str

    @property
    def internal_base_url(self) -> str:
        return f"http://{self.name}:8080/v1"

    def count(self, *, runner: CommandRunner | None = None) -> int:
        active_runner = run_command if runner is None else runner
        script = (
            "import json,urllib.request;"
            "value=json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8080/__phase10_count',timeout=3));"
            "print(value['count'])"
        )
        result = self.authority._run(
            self.authority.docker_command("exec", self.container_id, "python", "-c", script),
            runner=active_runner,
            timeout=15.0,
        )
        try:
            value = int(_text(result.stdout).strip())
        except ValueError as error:
            raise RuntimeError("counting provider returned a malformed count") from error
        if value < 0:
            raise RuntimeError("counting provider returned a negative count")
        return value

    def advertised_tools(self, *, runner: CommandRunner | None = None) -> tuple[str, ...]:
        active_runner = run_command if runner is None else runner
        script = (
            "import json,urllib.request;"
            "value=json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8080/__phase10_tools',timeout=3));"
            "print(json.dumps(value['tools'],separators=(',',':')))"
        )
        result = self.authority._run(
            self.authority.docker_command("exec", self.container_id, "python", "-c", script),
            runner=active_runner,
            timeout=15.0,
        )
        try:
            value = json.loads(_text(result.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("counting provider returned malformed advertised tools") from error
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise RuntimeError("counting provider returned malformed advertised tools")
        return tuple(value)

    def close(self, *, runner: CommandRunner | None = None) -> None:
        active_runner = run_command if runner is None else runner
        inspected = self.authority._run(
            self.authority.docker_command(
                "inspect", "--format", "{{json .Config.Labels}}", self.container_id
            ),
            runner=active_runner,
            timeout=30.0,
        )
        try:
            labels = json.loads(_text(inspected.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("counting provider labels are malformed") from error
        if (
            not isinstance(labels, dict)
            or labels.get("jhin.phase10.invocation") != self.authority.token
        ):
            raise RuntimeError("refusing to remove an unowned counting provider")
        removed = self.authority._run(
            self.authority.docker_command("rm", "-f", self.container_id),
            runner=active_runner,
            timeout=60.0,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError("failed to remove the owned counting provider")
        survivors = self.authority._exact_label_ids(
            runner=active_runner,
            resource="container",
            label=f"jhin.phase10.counting-provider={self.name}",
        )
        if survivors:
            raise RuntimeError("counting provider survived exact cleanup")


LIVE_SCENARIOS = {
    "boundary": LiveScenario(
        nodes=(
            "tests/integration/test_phase10_tool_worker_boundary.py::test_all_effect_classes_cross_tool_queue_once",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_advertised_tools_filter_before_reasoning",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_tool_queue_loss_blocks_effect_and_live_networks_are_isolated",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_live_sandbox_job_security_contract",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_worker_image_rejects_live_barrier_controls_in_production",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_agent_crash_matrix_retries_without_tool_effect_duplication",
            "tests/integration/test_phase10_tool_worker_boundary.py::test_tool_crash_matrix_preserves_claim_and_ambiguity_contract",
        ),
        expected_tests=11,
    ),
    "regressions": LiveScenario(
        nodes=(
            "tests/integration/test_phase3_exit.py",
            "tests/integration/test_phase6_exit.py",
            "tests/integration/test_phase7_exit.py",
            "tests/integration/test_phase9_exit.py",
        ),
        expected_tests=18,
    ),
    "socket-rootful": LiveScenario(
        nodes=(
            "tests/integration/test_phase10_sandbox_socket_modes.py::test_selected_socket_mode_live_boundary",
        ),
        expected_tests=1,
    ),
    "socket-rootless": LiveScenario(
        nodes=(
            "tests/integration/test_phase10_sandbox_socket_modes.py::test_selected_socket_mode_live_boundary",
        ),
        expected_tests=1,
    ),
    "wrong-gid": LiveScenario(
        nodes=(
            "tests/integration/test_phase10_sandbox_socket_modes.py::test_rootful_wrong_gid_fails_closed_without_socket_mutation",
        ),
        expected_tests=1,
        start_stack=False,
    ),
    "upgrade": LiveScenario(
        nodes=(
            "tests/integration/test_phase10_live_upgrade.py::test_inflight_phase9_histories_finish_after_phase10_swap",
        ),
        expected_tests=1,
        upgrade=True,
    ),
}


def build_live_pytest_command(scenario: LiveScenario) -> tuple[str, ...]:
    """Build the exact child command that bypasses repository marker defaults."""
    if scenario.expected_tests < 1 or not scenario.nodes:
        raise ValueError("live scenario requires an exact positive test selection")
    return (
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-m",
        "integration",
        *scenario.nodes,
        "-v",
    )


LIVE_FAILURE_OUTPUT_LIMIT = 64 * 1024
_SENSITIVE_ENV_NAME_PARTS = (
    "DSN",
    "KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "DATABASE_URL",
    "NATS_URL",
)


def _redact_live_failure_text(value: Any, environment: dict[str, str]) -> str:
    rendered = _text(value)
    sensitive_values = {
        item
        for name, item in environment.items()
        if item and any(part in name.upper() for part in _SENSITIVE_ENV_NAME_PARTS)
    }
    for sensitive_value in sorted(sensitive_values, key=len, reverse=True):
        rendered = rendered.replace(sensitive_value, "<redacted>")
    return rendered[-LIVE_FAILURE_OUTPUT_LIMIT:]


def _operational_container_fields(container: Mapping[str, Any]) -> dict[str, Any]:
    """Project Docker inspect data to bounded, non-configuration diagnostics."""
    config = container.get("Config")
    config = config if isinstance(config, Mapping) else {}
    labels = config.get("Labels")
    labels = labels if isinstance(labels, Mapping) else {}
    state = container.get("State")
    state = state if isinstance(state, Mapping) else {}
    health = state.get("Health")
    health = health if isinstance(health, Mapping) else {}
    raw_log = health.get("Log")
    health_log = raw_log[-10:] if isinstance(raw_log, list) else []
    return {
        "Id": container.get("Id"),
        "Name": container.get("Name"),
        "Image": container.get("Image"),
        "ConfigImage": config.get("Image"),
        "Labels": {
            key: labels.get(key)
            for key in (
                "com.docker.compose.project",
                "com.docker.compose.service",
                "jhin.sandbox.job",
            )
            if key in labels
        },
        "State": {
            key: state.get(key)
            for key in (
                "Status",
                "Running",
                "Restarting",
                "ExitCode",
                "OOMKilled",
                "Error",
                "StartedAt",
                "FinishedAt",
            )
        },
        "RestartCount": container.get("RestartCount"),
        "Health": {
            "Status": health.get("Status"),
            "Log": [
                {key: entry.get(key) for key in ("Start", "End", "ExitCode", "Output")}
                for entry in health_log
                if isinstance(entry, Mapping)
            ],
        },
    }


def emit_live_failure_output(
    error: subprocess.CalledProcessError,
    *,
    environment: dict[str, str],
    context: str,
) -> None:
    """Emit bounded, secret-redacted diagnostics for CI failures."""
    if context not in {"pytest", "setup", "stack-ps", "stack-logs"}:
        raise ValueError("live failure context is invalid")
    for label, value in (("stdout", error.stdout), ("stderr", error.stderr)):
        rendered = _redact_live_failure_text(value, environment)
        if not rendered:
            continue
        sys.stderr.write(f"\n--- live {context} {label} (redacted tail) ---\n")
        sys.stderr.write(rendered)
        if not rendered.endswith("\n"):
            sys.stderr.write("\n")


def emit_live_child_failure_output(
    error: subprocess.CalledProcessError,
    *,
    environment: dict[str, str],
) -> None:
    """Emit bounded, secret-redacted pytest diagnostics for CI failures."""
    emit_live_failure_output(error, environment=environment, context="pytest")


def run_command(
    command: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    check: bool,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Execute one bounded command, inheriting the caller's process group."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_bytes,
        capture_output=True,
        text=input_bytes is None,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _owned_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can transiently report EPERM while a killed orphan is being
        # adopted/reaped.  It is still a survivor until an ESRCH recheck.
        return True
    return True


def _signal_owned_process_group(process_group: int, signum: int) -> None:
    if process_group <= 1 or process_group == os.getpgrp():
        raise RuntimeError("refusing to signal a non-isolated process group")
    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return
    except PermissionError:
        # A concurrently dying Darwin group may be visible but unsignalable.
        # The bounded ESRCH proof below still decides whether it survived.
        return


_CATCHABLE_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_CATCHABLE_SIGNAL_SET = frozenset(_CATCHABLE_SIGNALS)


@dataclass
class _OwnedProcessPublication:
    """Caller-bound process handle admitted before its spawn window closes."""

    process: subprocess.Popen[Any] | None = None
    process_group: int | None = None
    pending_signum: int | None = None
    open: bool = True

    def publish(self, process: subprocess.Popen[Any]) -> None:
        if self.process is not None or not self.open:
            raise RuntimeError("owned process publication is inconsistent")
        self.process = process
        self.process_group = process.pid

    def defer(self, signum: int) -> None:
        if self.pending_signum is None:
            self.pending_signum = signum


_ACTIVE_OWNED_PROCESS_PUBLICATION: _OwnedProcessPublication | None = None


def _atomic_signal_handler_transition(handlers: Mapping[signal.Signals, Any]) -> None:
    """Change the catchable handler pair without exposing a half-transition."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE_SIGNAL_SET)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handlers}
    transition_errors: list[BaseException] = []
    for signum, handler in handlers.items():
        try:
            signal.signal(signum, handler)
        except BaseException as error:
            transition_errors.append(error)
            break
    if transition_errors:
        rollback_errors: list[BaseException] = []
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                rollback_errors.append(error)
        if rollback_errors:
            # A half-restored pair is safe only while both catchable signals
            # remain blocked.  The caller receives all transition evidence and
            # must not begin external or local cleanup.
            raise BaseExceptionGroup(
                "catchable signal handler transition and rollback failed",
                [*transition_errors, *rollback_errors],
            )
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            transition_errors.append(error)
        raise BaseExceptionGroup(
            "catchable signal handler transition failed",
            transition_errors,
        )
    errors: list[BaseException] = []
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    except BaseException as error:
        errors.append(error)
    if errors:
        raise BaseExceptionGroup("catchable signal handler transition failed", errors)


@dataclass
class _CatchableSignalLifecycle:
    """Prepared signal ownership whose mask stays blocked until activation."""

    previous_handlers: dict[signal.Signals, Any]
    previous_mask: set[int | signal.Signals]
    interrupted_signum: int | None = None
    state: Literal["prepared", "active", "closed"] = "prepared"

    @classmethod
    def prepare(cls) -> _CatchableSignalLifecycle:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE_SIGNAL_SET)
        previous_handlers = {signum: signal.getsignal(signum) for signum in _CATCHABLE_SIGNALS}
        lifecycle = cls(
            previous_handlers=previous_handlers,
            previous_mask=previous_mask,
        )
        try:
            for signum in _CATCHABLE_SIGNALS:
                signal.signal(signum, lifecycle.handle)
        except BaseException as install_error:
            restore_errors: list[BaseException] = []
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException as error:
                    restore_errors.append(error)
            if restore_errors:
                raise BaseExceptionGroup(
                    "catchable signal lifecycle installation and restoration failed",
                    [install_error, *restore_errors],
                ) from install_error
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except BaseException as error:
                raise BaseExceptionGroup(
                    "catchable signal lifecycle installation and mask restoration failed",
                    [install_error, error],
                ) from install_error
            raise
        return lifecycle

    def activate(self) -> None:
        if self.state != "prepared":
            return
        self.state = "active"
        signal.pthread_sigmask(signal.SIG_SETMASK, self.previous_mask)

    def handle(self, signum: int, frame: Any) -> None:
        del frame
        if self.interrupted_signum is not None:
            return
        self.interrupted_signum = signum
        publication = _ACTIVE_OWNED_PROCESS_PUBLICATION
        if publication is not None and publication.open:
            # A process-directed signal can be delivered to an unblocked
            # helper thread even while this spawning thread masks it.  Defer
            # raising until the caller has a handle/PGID it can exhaust.
            publication.defer(signum)
            return
        self.ignore()
        raise _LifecycleSignal(signum)

    def ignore(self) -> None:
        _atomic_signal_handler_transition(dict.fromkeys(_CATCHABLE_SIGNALS, signal.SIG_IGN))

    def restore(self) -> None:
        if self.state == "closed":
            return
        if self.state == "prepared":
            restore_errors: list[BaseException] = []
            for signum, handler in self.previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except BaseException as error:
                    restore_errors.append(error)
            if restore_errors:
                raise BaseExceptionGroup(
                    "prepared catchable signal lifecycle restoration failed",
                    restore_errors,
                )
            signal.pthread_sigmask(signal.SIG_SETMASK, self.previous_mask)
            self.state = "closed"
            return
        _atomic_signal_handler_transition(self.previous_handlers)
        self.state = "closed"


def _terminate_owned_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group: int,
) -> tuple[Any, Any]:
    """Terminate, reap, and prove absence of one isolated process group."""
    try:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE_SIGNAL_SET)
    except BaseException as mask_error:
        raise BaseExceptionGroup(
            "owned process-group termination could not acquire signal ownership",
            [mask_error, _OwnedProcessGroupSurvived(process_group)],
        ) from mask_error
    stdout: Any = None
    stderr: Any = None
    primary_error: BaseException | None = None
    absence_proven = False
    try:
        if _owned_process_group_exists(process_group):
            _signal_owned_process_group(process_group, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.output, error.stderr

        if _owned_process_group_exists(process_group):
            _signal_owned_process_group(process_group, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.output, error.stderr

        deadline = time.monotonic() + 5.0
        while _owned_process_group_exists(process_group):
            if time.monotonic() >= deadline:
                raise _OwnedProcessGroupSurvived(process_group)
            time.sleep(0.02)

        if process.poll() is None:
            try:
                stdout, stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired as error:
                raise _OwnedProcessGroupSurvived(process_group) from error
        absence_proven = True
    except BaseException as error:
        primary_error = error
    restore_error: BaseException | None = None
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    except BaseException as error:
        restore_error = error
    if restore_error is not None:
        primary_error = (
            restore_error
            if primary_error is None
            else BaseExceptionGroup(
                "owned process-group termination and signal restoration failed",
                [primary_error, restore_error],
            )
        )
    if primary_error is not None:
        if not absence_proven and not _contains_owned_process_group_survivor(primary_error):
            primary_error = BaseExceptionGroup(
                "owned process-group absence was not proven",
                [primary_error, _OwnedProcessGroupSurvived(process_group)],
            )
        raise primary_error
    return stdout, stderr


def run_owned_command(
    command: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    check: bool,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run one external command in an owned session and exhaust its descendants."""
    global _ACTIVE_OWNED_PROCESS_PUBLICATION

    catchable_signals = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, catchable_signals)
    publication = _OwnedProcessPublication()
    previous_publication = _ACTIVE_OWNED_PROCESS_PUBLICATION
    process: subprocess.Popen[Any] | None = None
    process_group: int | None = None
    mask_restored = False

    def restore_child_signal_state() -> None:
        for signum in _CATCHABLE_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def spawn_and_publish() -> subprocess.Popen[Any]:
        spawned = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_bytes is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=input_bytes is None,
            start_new_session=True,
            preexec_fn=restore_child_signal_state,
        )
        publication.publish(spawned)
        return spawned

    try:
        _ACTIVE_OWNED_PROCESS_PUBLICATION = publication
        try:
            process = spawn_and_publish()
            process_group = publication.process_group
        finally:
            publication.open = False
            _ACTIVE_OWNED_PROCESS_PUBLICATION = previous_publication

        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        mask_restored = True
        if publication.pending_signum is not None:
            raise _LifecycleSignal(publication.pending_signum)
        assert process_group is not None
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
        if _owned_process_group_exists(process_group):
            _terminate_owned_process_group(process, process_group=process_group)
        result = subprocess.CompletedProcess(
            command,
            cast(int, process.returncode),
            stdout,
            stderr,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result
    except subprocess.TimeoutExpired as error:
        process = publication.process
        process_group = publication.process_group
        if process is None or process_group is None:
            raise
        try:
            stdout, stderr = _terminate_owned_process_group(
                process,
                process_group=process_group,
            )
        except BaseException as teardown_error:
            raise BaseExceptionGroup(
                "live child timed out and its process group survived",
                [error, teardown_error],
            ) from error
        error.output = stdout
        error.stderr = stderr
        raise
    except BaseException as error:
        process = publication.process
        process_group = publication.process_group
        if process is None or process_group is None:
            raise
        try:
            _terminate_owned_process_group(process, process_group=process_group)
        except BaseException as teardown_error:
            raise BaseExceptionGroup(
                "live child failed and its process group survived",
                [error, teardown_error],
            ) from error
        raise
    finally:
        publication.open = False
        if _ACTIVE_OWNED_PROCESS_PUBLICATION is publication:
            _ACTIVE_OWNED_PROCESS_PUBLICATION = previous_publication
        if not mask_restored:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


@dataclass(frozen=True)
class SocketMetadata:
    path: Path
    inode: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def capture(cls, path: Path) -> SocketMetadata:
        if not path.is_absolute():
            raise ValueError("Docker socket path must be absolute")
        metadata = path.lstat()
        return cls(
            path=path,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            uid=metadata.st_uid,
            gid=metadata.st_gid,
        )


def validate_socket_metadata(
    metadata: SocketMetadata,
    *,
    mode: str,
    expected_gid: int | None = None,
) -> SocketMetadata:
    """Fail closed on a mismatched rootful/rootless Unix-socket authority."""
    selected = _mode(mode)
    if not metadata.path.is_absolute() or not stat.S_ISSOCK(metadata.mode):
        raise ValueError("selected Docker authority is not an absolute Unix socket")
    permissions = stat.S_IMODE(metadata.mode)
    if selected == "rootful":
        if metadata.uid != 0:
            raise ValueError("rootful Docker socket must be root-owned")
        if expected_gid is None or expected_gid <= 0 or metadata.gid != expected_gid:
            raise ValueError("rootful Docker socket GID does not match the selected authority")
        if permissions & 0o060 != 0o060:
            raise ValueError("rootful Docker socket group must have read/write access")
    else:
        if metadata.uid != 10001:
            raise ValueError("rootless Docker socket must have host UID 10001")
        if expected_gid is not None:
            raise ValueError("rootless Docker authority cannot carry a rootful GID")
    return metadata


def validate_daemon_info(info: Mapping[str, Any], *, mode: str) -> None:
    """Bind the requested mode to the selected daemon's reported authority."""
    selected = _mode(mode)
    raw_options = info.get("SecurityOptions", [])
    if not isinstance(raw_options, list) or any(not isinstance(item, str) for item in raw_options):
        raise ValueError("Docker daemon security options are malformed")
    is_rootless = any(item.split(",", 1)[0] == "name=rootless" for item in raw_options)
    if selected == "rootful" and is_rootless:
        raise ValueError("rootful mode selected a rootless daemon")
    if selected == "rootless":
        if not is_rootless:
            raise ValueError("rootless mode requires a rootless daemon")
        if str(info.get("CgroupVersion")) != "2":
            raise ValueError("rootless acceptance requires cgroup v2")
        if info.get("CgroupDriver") != "systemd":
            raise ValueError("rootless acceptance requires the systemd cgroup driver")


def select_live_authority(
    *,
    repo: Path,
    mode: str,
    source_environment: Mapping[str, str] | None = None,
) -> ComposeAuthority:
    """Bind one live invocation to an immutable, snapshotted Unix socket."""
    selected = _mode(mode)
    source = dict(os.environ if source_environment is None else source_environment)
    if selected == "rootful":
        socket_path = Path(source.get("PHASE10_ROOTFUL_DOCKER_SOCKET", "/var/run/docker.sock"))
        raw_gid = source.get("SANDBOX_DOCKER_GID")
        if raw_gid is None or not raw_gid.isdecimal() or int(raw_gid) <= 0:
            raise ValueError("rootful selection requires a positive SANDBOX_DOCKER_GID")
        socket_gid: int | None = int(raw_gid)
    else:
        raw_socket = source.get("PHASE10_ROOTLESS_DOCKER_SOCKET")
        if not raw_socket:
            raise ValueError("rootless selection requires PHASE10_ROOTLESS_DOCKER_SOCKET")
        socket_path = Path(raw_socket)
        socket_gid = None
    metadata = validate_socket_metadata(
        SocketMetadata.capture(socket_path),
        mode=selected,
        expected_gid=socket_gid,
    )
    return ComposeAuthority.create(
        repo=repo,
        mode=selected,
        socket_path=socket_path,
        socket_gid=socket_gid,
        source_environment=source,
        socket_snapshot=metadata,
    )


def _mode(value: str) -> ComposeMode:
    if value not in {"rootful", "rootless"}:
        raise ValueError(_MODE_ERROR)
    return cast(ComposeMode, value)


def compose_files_for(mode: str, *, upgrade: bool = False) -> tuple[str, ...]:
    """Return the only accepted base/dev/socket-mode Compose vector."""
    selected = _mode(mode)
    files = ("compose.yaml", "compose.dev.yaml", f"compose.{selected}.yaml")
    if upgrade:
        return (*files, "tests/integration/compose.phase10-upgrade.yaml")
    return files


def sanitized_external_environment(
    source: Mapping[str, str],
    *,
    docker_host: str,
    mode: str,
    values: Mapping[str, str],
) -> dict[str, str]:
    """Remove ambient target selectors before installing one authority.

    ``DOCKER_CONFIG`` is intentionally retained for registry authentication;
    context, TLS, BuildKit, Compose, socket-mode, and barrier selectors are not.
    """
    selected = _mode(mode)
    if not docker_host.startswith("unix://"):
        raise ValueError("Docker authority must be an explicit unix socket")
    clean = {
        key: value
        for key, value in source.items()
        if key in _PASSTHROUGH_ENVIRONMENT
        and key not in _TARGET_ENVIRONMENT
        and not any(key.startswith(prefix) for prefix in _TARGET_PREFIXES)
    }
    clean.update(values)
    clean.update(
        {
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "DOCKER_HOST": docker_host,
            "BUILDX_BUILDER": "default",
            "PHASE10_SOCKET_MODE": selected,
        }
    )
    clean.pop("BUILDKIT_HOST", None)
    clean.pop("DOCKER_CONTEXT", None)
    return clean


def _decode_compose_rows(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        raise ComposePsError("Compose ps output is blank")
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        decoded_rows: list[Any] = []
        try:
            for line in output.splitlines():
                if line.strip():
                    decoded_rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ComposePsError("Compose ps output is malformed JSON") from error
        decoded = decoded_rows
    if isinstance(decoded, dict):
        rows: list[Any] = [decoded]
    elif isinstance(decoded, list):
        rows = decoded
    else:
        raise ComposePsError("Compose ps output is malformed: expected objects")
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ComposePsError("Compose ps output is malformed: expected nonempty objects")
    return [cast(dict[str, Any], row) for row in rows]


def parse_compose_ps(
    output: str,
    expected_services: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Parse array/object/NDJSON Compose output and enforce exact readiness."""
    expected = set(expected_services)
    parsed: dict[str, dict[str, Any]] = {}
    for row in _decode_compose_rows(output):
        service = row.get("Service")
        if not isinstance(service, str) or not service:
            raise ComposePsError("Compose ps row is malformed: missing Service")
        if service in parsed:
            raise ComposePsError(f"Compose ps contains duplicate service {service}")
        parsed[service] = row
    present = set(parsed)
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if missing:
        raise ComposePsError(f"Compose ps is missing services: {missing}")
    if unexpected:
        raise ComposePsError(f"Compose ps contains unexpected services: {unexpected}")
    starting_services: list[str] = []
    for service, row in parsed.items():
        state = str(row.get("State", "")).lower()
        if state != "running":
            raise ComposePsError(f"service {service} is not running: {state or 'missing'}")
        health_value = row.get("Health", "")
        health = "" if health_value is None else str(health_value).lower()
        if health == "starting":
            starting_services.append(service)
            continue
        if health != "healthy":
            raise ComposePsError(f"service {service} is unhealthy: {health or 'missing health'}")
    if starting_services:
        raise ComposeStartingError(f"services are still starting: {sorted(starting_services)}")
    return parsed


def _parse_worker_recovery_inventory(
    output: str,
    expected_services: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Require an exact Compose inventory with one stable container ID per service."""
    expected = set(expected_services)
    parsed: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for row in _decode_compose_rows(output):
        service = row.get("Service")
        if not isinstance(service, str) or not service:
            raise ComposePsError("Compose ps row is malformed: missing Service")
        if service in parsed:
            duplicates.add(service)
            continue
        identifier = row.get("ID")
        if not isinstance(identifier, str) or not identifier:
            raise ComposePsError(f"Compose ps service {service} is missing its container ID")
        parsed[service] = row
    present = set(parsed)
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if missing or unexpected or duplicates:
        reasons = []
        if missing:
            reasons.append("missing services")
        if unexpected:
            reasons.append("unexpected services")
        if duplicates:
            reasons.append("duplicate services")
        raise ComposePsError(
            f"Compose ps inventory mismatch ({', '.join(reasons)}): "
            f"expected={sorted(expected)}, present={sorted(present)}, missing={missing}, "
            f"unexpected={unexpected}, duplicate={sorted(duplicates)}"
        )
    return parsed


def _classify_worker_recovery(
    output: str,
    *,
    expected_services: Iterable[str],
    fixed_identities: Mapping[str, str],
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Accept only fully ready rows or the two bounded restart transitions."""
    parsed = _parse_worker_recovery_inventory(output, expected_services)
    changed = sorted(
        service
        for service, expected_identifier in fixed_identities.items()
        if parsed[service]["ID"] != expected_identifier
    )
    if changed:
        raise ComposePsError(f"worker recovery identity changed for services: {changed}")

    transitional: list[str] = []
    terminal: list[str] = []
    for service in sorted(parsed):
        row = parsed[service]
        state = str(row.get("State", "")).lower()
        health_value = row.get("Health")
        health = "" if health_value is None else str(health_value).lower()
        if state == "running" and health == "healthy":
            continue
        if state == "restarting":
            transitional.append(f"{service}=restarting/{health or 'missing-health'}")
            continue
        if state == "running" and health == "starting":
            transitional.append(f"{service}=running/starting")
            continue
        terminal.append(f"{service}={state or 'missing-state'}/{health or 'missing-health'}")
    if terminal:
        raise ComposePsError(
            "worker recovery contains terminal service states: " + ", ".join(terminal)
        )
    return parsed, tuple(transitional)


def parse_compose_port(output: str) -> int:
    """Return one Docker-allocated published port from ``compose port``."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("expected exactly one published port")
    match = re.fullmatch(r"(?:\[[^]]+\]|[^:]+):(\d+)", lines[0])
    if match is None:
        raise ValueError("invalid published port output")
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        raise ValueError("published port was not allocated")
    return port


@dataclass(frozen=True)
class BarrierRoot:
    """An unpredictable, test-only, cross-UID crash-barrier directory."""

    root: Path
    failpoint: str

    @property
    def selected_dir(self) -> Path:
        return self.root / self.failpoint

    def _validate(self) -> None:
        root_stat = self.root.lstat()
        selected_stat = self.selected_dir.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(selected_stat.st_mode):
            raise RuntimeError("barrier path was replaced")
        if self.root.parent != Path("/tmp") or self.root.is_symlink():
            raise RuntimeError("barrier root escaped /tmp")
        if self.selected_dir.is_symlink():
            raise RuntimeError("barrier failpoint directory was replaced")

    def release(self, identity: str) -> Path:
        self._validate()
        if re.fullmatch(r"[0-9a-f-]{36}", identity) is None:
            raise ValueError("barrier identity must be a lowercase UUID")
        path = self.selected_dir / f"{identity}.release"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        try:
            os.write(descriptor, b"release\n")
            os.fchmod(descriptor, 0o666)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self.selected_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return path

    def wait_arrival(self, *, timeout: float) -> str:
        if timeout <= 0:
            raise ValueError("barrier arrival timeout must be positive")
        deadline = time.monotonic() + timeout
        while True:
            self._validate()
            entries = list(self.selected_dir.iterdir())
            invalid = [
                path
                for path in entries
                if path.is_symlink() or re.fullmatch(r"[0-9a-f-]{36}\.arrived", path.name) is None
            ]
            if invalid:
                raise RuntimeError("unexpected barrier marker")
            if len(entries) > 1:
                raise RuntimeError("multiple barrier arrivals are ambiguous")
            if entries:
                marker = entries[0]
                metadata = marker.lstat()
                mode = stat.S_IMODE(metadata.st_mode)
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("barrier arrival marker is malformed")
                if mode == 0o644:
                    if marker.read_bytes() != b"arrived\n":
                        raise RuntimeError("barrier arrival marker is malformed")
                    return marker.name.removesuffix(".arrived")
                if mode != 0o600:
                    raise RuntimeError("barrier arrival marker mode is invalid")
            if time.monotonic() >= deadline:
                raise TimeoutError("worker did not reach the selected crash barrier")
            time.sleep(0.02)

    def cleanup(self) -> None:
        if not self.root.exists():
            return
        self._validate()
        os.chmod(self.root, 0o700, follow_symlinks=False)
        shutil.rmtree(self.root)


def _validate_child_barrier_journal(path: Path) -> os.stat_result:
    if (
        not path.is_absolute()
        or path.name != _CHILD_BARRIER_JOURNAL_NAME
        or path.parent.parent != Path("/tmp")
        or re.fullmatch(r"jhin-p10-runtime-[0-9a-f]{8,16}-[A-Za-z0-9_]+", path.parent.name) is None
    ):
        raise RuntimeError("child barrier journal escaped its leased runtime directory")
    runtime_metadata = path.parent.lstat()
    if (
        stat.S_ISLNK(runtime_metadata.st_mode)
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != os.getuid()
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o711
    ):
        raise RuntimeError("child barrier journal runtime identity changed")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("child barrier journal is not owner-only")
    return metadata


def _append_child_barrier_intent(journal: Path, *, root: Path, failpoint: str) -> None:
    expected_journal = _validate_child_barrier_journal(journal)
    if (
        root.parent != Path("/tmp")
        or re.fullmatch(r"jhin-p10-barrier-[0-9a-f]{32}", root.name) is None
    ):
        raise RuntimeError("child barrier intent path is invalid")
    payload = (
        json.dumps(
            {"failpoint": failpoint, "root": str(root)},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_dev != expected_journal.st_dev
            or metadata.st_ino != expected_journal.st_ino
        ):
            raise RuntimeError("child barrier journal is not owner-only")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            _write_all(descriptor, payload, description="child barrier journal")
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    directory = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def create_barrier_root(failpoint: str) -> BarrierRoot:
    if not failpoint or "/" in failpoint or failpoint in {".", ".."}:
        raise ValueError("invalid barrier failpoint")
    raw_journal = os.environ.get(_CHILD_BARRIER_JOURNAL_ENV)
    if raw_journal is None:
        root = Path(tempfile.mkdtemp(prefix="jhin-p10-barrier-", dir="/tmp"))
        selected = root / failpoint
        try:
            selected.mkdir(mode=0o700)
            os.chmod(selected, 0o1777, follow_symlinks=False)
            os.chmod(root, 0o711, follow_symlinks=False)
        except BaseException:
            os.chmod(root, 0o700, follow_symlinks=False)
            shutil.rmtree(root)
            raise
        return BarrierRoot(root=root, failpoint=failpoint)

    journal = Path(raw_journal)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE_SIGNAL_SET)
    root = Path("/tmp") / f"jhin-p10-barrier-{secrets.token_hex(16)}"
    selected = root / failpoint
    root_created = False
    try:
        try:
            root.lstat()
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("unpredictable child barrier path already exists")
        # Publish and fsync the exact cleanup intent before the first mkdir.
        # A pending outer signal is delivered only after both journal and
        # directory construction have transferred ownership to the parent.
        _append_child_barrier_intent(journal, root=root, failpoint=failpoint)
        root.mkdir(mode=0o700)
        root_created = True
        selected.mkdir(mode=0o700)
        os.chmod(selected, 0o1777, follow_symlinks=False)
        os.chmod(root, 0o711, follow_symlinks=False)
        return BarrierRoot(root=root, failpoint=failpoint)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        if root_created:
            try:
                os.chmod(root, 0o700, follow_symlinks=False)
                shutil.rmtree(root)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "child barrier creation and rollback failed",
                [error, *cleanup_errors],
            ) from error
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _events(history_or_events: Any) -> Sequence[Any]:
    events = getattr(history_or_events, "events", history_or_events)
    if not isinstance(events, Sequence):
        events = tuple(events)
    return cast(Sequence[Any], events)


def _present_event_attributes(event: Any, field: str) -> Any:
    has_field = getattr(event, "HasField", None)
    if callable(has_field) and not has_field(field):
        return None
    return getattr(event, field, None)


def activity_schedule_pairs(history_or_events: Any) -> list[tuple[str, str]]:
    """Extract scheduled activity name/queue pairs in history order."""
    pairs: list[tuple[str, str]] = []
    for event in _events(history_or_events):
        attributes = _present_event_attributes(
            event,
            "activity_task_scheduled_event_attributes",
        )
        if attributes is None:
            continue
        activity_type = getattr(attributes, "activity_type", None)
        task_queue = getattr(attributes, "task_queue", None)
        name = getattr(activity_type, "name", None)
        queue = getattr(task_queue, "name", None)
        if not isinstance(name, str) or not name or not isinstance(queue, str) or not queue:
            raise ValueError("scheduled activity history is malformed")
        pairs.append((name, queue))
    return pairs


def activity_start_count(history_or_events: Any, activity_name: str) -> int:
    """Count recorded final Started events, not retry attempts."""
    scheduled_ids: set[int] = set()
    events = _events(history_or_events)
    for event in events:
        attributes = _present_event_attributes(
            event,
            "activity_task_scheduled_event_attributes",
        )
        if attributes is None:
            continue
        name = getattr(getattr(attributes, "activity_type", None), "name", None)
        event_id = getattr(event, "event_id", None)
        if name == activity_name:
            if not isinstance(event_id, int):
                raise ValueError("scheduled activity event ID is malformed")
            scheduled_ids.add(event_id)
    count = 0
    for event in events:
        attributes = _present_event_attributes(
            event,
            "activity_task_started_event_attributes",
        )
        scheduled_event_id = getattr(attributes, "scheduled_event_id", None)
        if scheduled_event_id in scheduled_ids:
            count += 1
    return count


def activity_attempts(history_or_events: Any, activity_name: str) -> list[int]:
    """Return final attempt numbers for exact activity schedules in order."""
    events = _events(history_or_events)
    schedule_order: list[int] = []
    schedule_names: dict[int, str] = {}
    for event in events:
        attributes = _present_event_attributes(
            event,
            "activity_task_scheduled_event_attributes",
        )
        if attributes is None:
            continue
        event_id = getattr(event, "event_id", None)
        name = getattr(getattr(attributes, "activity_type", None), "name", None)
        if (
            type(event_id) is not int
            or event_id < 1
            or event_id in schedule_names
            or not isinstance(name, str)
            or not name
        ):
            raise ValueError("scheduled activity correlation is malformed")
        schedule_order.append(event_id)
        schedule_names[event_id] = name

    started_attempts: dict[int, int] = {}
    for event in events:
        attributes = _present_event_attributes(
            event,
            "activity_task_started_event_attributes",
        )
        if attributes is None:
            continue
        scheduled_event_id = getattr(attributes, "scheduled_event_id", None)
        if type(scheduled_event_id) is not int or scheduled_event_id not in schedule_names:
            raise ValueError("activity Started event references an unknown schedule")
        if scheduled_event_id in started_attempts:
            raise ValueError("activity schedule has duplicate Started events")
        attempt = getattr(attributes, "attempt", None)
        if type(attempt) is not int or attempt < 1:
            raise ValueError("activity Started attempt is malformed")
        started_attempts[scheduled_event_id] = attempt

    selected = [
        schedule_id
        for schedule_id in schedule_order
        if schedule_names[schedule_id] == activity_name
    ]
    missing = [schedule_id for schedule_id in selected if schedule_id not in started_attempts]
    if missing:
        raise ValueError(f"activity schedules are missing Started events: {missing}")
    return [started_attempts[schedule_id] for schedule_id in selected]


def read_phase9_source_ref(
    repo: Path,
    *,
    runner: CommandRunner = run_command,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Load the frozen Phase 9 commit and prove it is an ancestor of HEAD."""
    path = repo / "packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt"
    source_ref = path.read_text(encoding="utf-8").strip()
    if _HEX_REF.fullmatch(source_ref) is None:
        raise ValueError("Phase 9 source ref must be exactly forty lowercase hex characters")
    selected_environment = dict(os.environ if environment is None else environment)
    resolved = runner(
        ("git", "cat-file", "-e", f"{source_ref}^{{commit}}"),
        env=selected_environment,
        cwd=repo,
        timeout=30.0,
        check=False,
        input_bytes=None,
    )
    ancestor = runner(
        ("git", "merge-base", "--is-ancestor", source_ref, "HEAD"),
        env=selected_environment,
        cwd=repo,
        timeout=30.0,
        check=False,
        input_bytes=None,
    )
    if resolved.returncode != 0 or ancestor.returncode != 0:
        raise ValueError("Phase 9 source ref is not an available ancestor commit")
    return source_ref


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _new_direct_tmp(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    os.chmod(path, 0o711, follow_symlinks=False)
    return path


@dataclass(frozen=True)
class ComposeAuthority:
    """One immutable daemon/project/vector/environment ownership boundary."""

    repo: Path
    mode: ComposeMode
    socket_path: Path
    socket_gid: int | None
    token: str
    project: str
    sandbox_network: str
    runner_image: str
    sandbox_image: str
    runtime_dir: Path
    master_key_path: Path
    barrier_root: Path
    docker_executable: str
    _environment_items: tuple[tuple[str, str], ...]
    _published_port_items: tuple[tuple[str, int], ...] = ()
    socket_snapshot: SocketMetadata | None = None
    _allow_missing_recovery_paths: bool = False

    @classmethod
    def create(
        cls,
        *,
        repo: Path,
        mode: str,
        socket_path: Path,
        socket_gid: int | None = None,
        token: str | None = None,
        source_environment: Mapping[str, str] | None = None,
        socket_snapshot: SocketMetadata | None = None,
    ) -> ComposeAuthority:
        selected = _mode(mode)
        identifier = token if token is not None else secrets.token_hex(6)
        if _TOKEN.fullmatch(identifier) is None:
            raise ValueError("authority token must be 8-16 lowercase hexadecimal characters")
        absolute_repo = repo.resolve(strict=True)
        if not (absolute_repo / "compose.yaml").is_file():
            raise ValueError("authority repository does not contain compose.yaml")
        if not socket_path.is_absolute():
            raise ValueError("Docker socket path must be absolute")
        if selected == "rootful" and (socket_gid is None or socket_gid <= 0):
            raise ValueError("rootful authority requires a positive socket GID")
        if selected == "rootless" and socket_gid is not None:
            raise ValueError("rootless authority cannot carry a socket GID")

        runtime_dir = _new_direct_tmp(f"jhin-p10-runtime-{identifier}-")
        barrier_root: Path | None = None
        try:
            barrier_root = _new_direct_tmp(f"jhin-p10-barriers-{identifier}-")
            project = f"jhin-p10-{identifier}"
            sandbox_network = f"jhin-p10-sandbox-{identifier}"
            runner_image = f"jhin-phase10-sandbox-runner:{identifier}"
            sandbox_image = f"jhin-phase10-sandbox:{identifier}"
            master_key_path = runtime_dir / "jhin_master_key"
            values = {
                "APP_ENV": "test",
                "COMPOSE_PROJECT_NAME": project,
                "SANDBOX_NETWORK": sandbox_network,
                "SANDBOX_RUNNER_IMAGE": runner_image,
                "SANDBOX_DEFAULT_IMAGE": sandbox_image,
                "SANDBOX_RUNNER_TOKEN": f"phase10-runner-{identifier}",
                "MASTER_KEY_FILE_HOST": str(master_key_path),
                "JHIN_TEST_CRASH_BARRIER_HOST_DIR": str(barrier_root),
                "JHIN_TEST_CRASH_BARRIER_DIR": "",
                "JHIN_TEST_CRASH_BARRIER_NAME": "",
                "JHIN_TEST_CRASH_BARRIER_MATCH": "",
                "WEB_PORT": "127.0.0.1:0",
                "API_PORT": "127.0.0.1:0",
            }
            for variable, _service, _container_port in PUBLISHED_ENDPOINTS:
                values.setdefault(variable, "0")
            if selected == "rootful":
                assert socket_gid is not None
                values.update(
                    {
                        "SANDBOX_DOCKER_SOCKET_HOST": str(socket_path),
                        "SANDBOX_DOCKER_GID": str(socket_gid),
                    }
                )
            else:
                values["PHASE10_ROOTLESS_DOCKER_SOCKET"] = str(socket_path)
            environment = sanitized_external_environment(
                source_environment if source_environment is not None else os.environ,
                docker_host=f"unix://{socket_path}",
                mode=selected,
                values=values,
            )
            executable = shutil.which("docker")
            if executable is None:
                # Command construction remains testable on non-Docker unit hosts;
                # live preflight rejects a missing executable before mutation.
                executable = "/usr/bin/docker"
            return cls(
                repo=absolute_repo,
                mode=selected,
                socket_path=socket_path,
                socket_gid=socket_gid,
                token=identifier,
                project=project,
                sandbox_network=sandbox_network,
                runner_image=runner_image,
                sandbox_image=sandbox_image,
                runtime_dir=runtime_dir,
                master_key_path=master_key_path,
                barrier_root=barrier_root,
                docker_executable=executable,
                _environment_items=tuple(sorted(environment.items())),
                socket_snapshot=socket_snapshot,
            )
        except BaseException:
            for path in (barrier_root, runtime_dir):
                if path is not None and path.exists():
                    os.chmod(path, 0o700, follow_symlinks=False)
                    shutil.rmtree(path)
            raise

    @property
    def environment(self) -> dict[str, str]:
        return dict(self._environment_items)

    @property
    def child_barrier_journal_path(self) -> Path:
        return self.runtime_dir / _CHILD_BARRIER_JOURNAL_NAME

    def with_child_barrier_journal(self) -> ComposeAuthority:
        """Prepublish the fsynced child-barrier recovery journal."""
        environment = self.environment
        existing = environment.get(_CHILD_BARRIER_JOURNAL_ENV)
        expected = self.child_barrier_journal_path
        if existing is not None:
            if Path(existing) != expected:
                raise RuntimeError("child barrier journal authority changed")
            _validate_child_barrier_journal(expected)
            return self

        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CATCHABLE_SIGNAL_SET)
        descriptor = -1
        created_stat: os.stat_result | None = None
        primary_error: BaseException | None = None
        prepared: ComposeAuthority | None = None
        try:
            descriptor = os.open(
                expected,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            created_stat = os.fstat(descriptor)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            directory = os.open(
                self.runtime_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            environment[_CHILD_BARRIER_JOURNAL_ENV] = str(expected)
            prepared = replace(
                self,
                _environment_items=tuple(sorted(environment.items())),
            )
        except BaseException as error:
            primary_error = error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except BaseException as error:
                primary_error = (
                    error
                    if primary_error is None
                    else BaseExceptionGroup(
                        "child barrier journal setup and signal restoration failed",
                        [primary_error, error],
                    )
                )
        if primary_error is not None:
            rollback_errors: list[BaseException] = []
            if created_stat is not None:
                try:
                    _unlink_exact_file(
                        expected,
                        device=created_stat.st_dev,
                        inode=created_stat.st_ino,
                    )
                except BaseException as error:
                    rollback_errors.append(error)
            if rollback_errors:
                raise BaseExceptionGroup(
                    "child barrier journal setup and rollback failed",
                    [primary_error, *rollback_errors],
                ) from primary_error
            raise primary_error
        assert prepared is not None
        return prepared

    @property
    def published_ports(self) -> dict[str, int]:
        return dict(self._published_port_items)

    def with_published_ports(self, ports: Mapping[str, int]) -> ComposeAuthority:
        expected = {variable for variable, _service, _port in PUBLISHED_ENDPOINTS}
        if set(ports) != expected or len(set(ports.values())) != len(ports):
            raise ValueError("resolved port inventory is incomplete or colliding")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports.values()):
            raise ValueError("resolved port inventory contains an invalid port")
        return replace(self, _published_port_items=tuple(sorted(ports.items())))

    def with_upgrade_runtime(self, frozen: FrozenPhase9Image) -> ComposeAuthority:
        if (
            _HEX_REF.fullmatch(frozen.source_ref) is None
            or frozen.tag != self.phase9_image_tag(frozen.source_ref)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", frozen.image_id) is None
        ):
            raise ValueError("frozen Phase 9 image identity is invalid")
        environment = self.environment
        environment.update(
            {
                "PHASE9_AGENT_IMAGE": frozen.image_id,
                "PHASE10_UPGRADE_PHASE9_TAG": frozen.tag,
                "PHASE10_UPGRADE_SOURCE_REF": frozen.source_ref,
            }
        )
        created: list[BarrierRoot] = []
        try:
            for scenario in _UPGRADE_SCENARIOS:
                barrier = create_barrier_root(_UPGRADE_FAILPOINTS[scenario])
                created.append(barrier)
                upper = scenario.upper()
                environment[f"PHASE10_UPGRADE_NAMESPACE_{upper}"] = (
                    f"jhin-p10-{self.token}-{scenario}-{secrets.token_hex(4)}"
                )
                environment[f"PHASE10_UPGRADE_BARRIER_{upper}_HOST"] = str(barrier.root)
                environment[f"PHASE10_UPGRADE_BARRIER_{upper}_NAME"] = (
                    "" if scenario == "approval" else barrier.failpoint
                )
                environment[f"PHASE10_UPGRADE_BARRIER_{upper}_MATCH"] = ""
        except BaseException:
            for barrier in created:
                barrier.cleanup()
            raise
        return replace(self, _environment_items=tuple(sorted(environment.items())))

    def with_rootful_socket_gid(self, socket_gid: int) -> ComposeAuthority:
        if self.mode != "rootful" or type(socket_gid) is not int or socket_gid <= 0:
            raise ValueError("rootful socket GID override must be a positive integer")
        environment = self.environment
        environment["SANDBOX_DOCKER_GID"] = str(socket_gid)
        return replace(
            self,
            socket_gid=socket_gid,
            _environment_items=tuple(sorted(environment.items())),
        )

    @property
    def files(self) -> tuple[str, ...]:
        return compose_files_for(self.mode)

    @property
    def expected_services(self) -> set[str]:
        if self.mode == "rootless":
            return set(EXPECTED_ROOTLESS_SERVICES)
        return set(EXPECTED_ROOTFUL_SERVICES)

    @property
    def docker_host(self) -> str:
        return f"unix://{self.socket_path}"

    def docker_command(self, *args: str) -> tuple[str, ...]:
        return (self.docker_executable, "--host", self.docker_host, *args)

    def compose_auto_image_tags(self, *, upgrade: bool = False) -> tuple[str, ...]:
        """Return every unique-project tag Compose assigns to an untagged build."""
        services: tuple[str, ...] = _BASE_COMPOSE_AUTO_IMAGE_SERVICES
        if upgrade:
            services = (*services, *_UPGRADE_COMPOSE_AUTO_IMAGE_SERVICES)
        return tuple(f"{self.project}-{service}" for service in services)

    def phase9_image_tag(self, source_ref: str) -> str:
        if _HEX_REF.fullmatch(source_ref) is None:
            raise ValueError("Phase 9 image source ref is malformed")
        return f"jhin-phase9-agent-worker:{source_ref[:12]}-{self.token}"

    @staticmethod
    def phase9_archive_command(source_ref: str) -> tuple[str, ...]:
        if _HEX_REF.fullmatch(source_ref) is None:
            raise ValueError("Phase 9 archive source ref is malformed")
        return ("git", "archive", "--format=tar", source_ref)

    def phase9_build_command(self, source_ref: str) -> tuple[str, ...]:
        return self.docker_command(
            "build",
            "-f",
            "docker/python.Dockerfile",
            "--build-arg",
            "SERVICE_PACKAGE=jhin-agent-worker",
            "-t",
            self.phase9_image_tag(source_ref),
            "-",
        )

    def build_phase9_agent_image(
        self,
        source_ref: str,
        *,
        runner: CommandRunner = run_command,
    ) -> FrozenPhase9Image:
        """Feed one bounded committed archive into one bounded selected-daemon build."""
        archive = runner(
            self.phase9_archive_command(source_ref),
            env=self.environment,
            cwd=self.repo,
            timeout=120.0,
            check=False,
            input_bytes=b"",
        )
        if archive.returncode != 0:
            raise RuntimeError("failed to archive the frozen Phase 9 source")
        if not isinstance(archive.stdout, bytes) or not archive.stdout:
            raise RuntimeError("Phase 9 archive did not produce a tar stream")
        tag = self.phase9_image_tag(source_ref)
        try:
            built = self._run(
                self.phase9_build_command(source_ref),
                runner=runner,
                timeout=1200.0,
                check=False,
                input_bytes=archive.stdout,
            )
            if built.returncode != 0:
                raise RuntimeError("failed to build the frozen Phase 9 agent image")
            inspected = self._run(
                self.docker_command("image", "inspect", "--format", "{{.Id}}", tag),
                runner=runner,
                timeout=30.0,
            )
            ids = [line.strip() for line in _text(inspected.stdout).splitlines() if line.strip()]
            if len(ids) != 1 or re.fullmatch(r"sha256:[0-9a-f]{64}", ids[0]) is None:
                raise RuntimeError("frozen Phase 9 image ID is malformed")
            return FrozenPhase9Image(source_ref=source_ref, tag=tag, image_id=ids[0])
        except BaseException as build_error:
            if _contains_owned_process_group_survivor(build_error):
                # The build group can still be mutating the selected daemon.
                # No inspect/remove command is safe until a recovery operator
                # has conclusively exhausted that group.
                raise
            cleanup_error: BaseException | None = None
            try:
                self.remove_phase9_agent_image(source_ref, runner=runner)
            except BaseException as error:
                cleanup_error = error
            if cleanup_error is not None:
                raise BaseExceptionGroup(
                    "frozen Phase 9 build failed and exact tag survivor state is unknown",
                    [build_error, cleanup_error],
                ) from build_error
            raise

    def remove_phase9_agent_image(
        self,
        source_ref: str,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        """Inspect, remove, and independently prove absence of one derived frozen tag."""
        tag = self.phase9_image_tag(source_ref)
        present = self._run(
            self.docker_command("image", "inspect", tag),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if present.returncode == 1:
            return
        if present.returncode != 0:
            raise RuntimeError("failed to inspect frozen Phase 9 image; survivor state is unknown")
        removed = self._run(
            self.docker_command("image", "rm", tag),
            runner=runner,
            timeout=120.0,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError("failed to remove frozen Phase 9 image; survivor state is unknown")
        absent = self._run(
            self.docker_command("image", "inspect", tag),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if absent.returncode != 1:
            raise RuntimeError("frozen Phase 9 image remains after exact cleanup")

    def compose_command(self, *args: str, upgrade: bool = False) -> tuple[str, ...]:
        command = [
            self.docker_executable,
            "--host",
            self.docker_host,
            "compose",
            "-p",
            self.project,
        ]
        for filename in compose_files_for(self.mode, upgrade=upgrade):
            command.extend(("-f", filename))
        command.extend(args)
        return tuple(command)

    def _child_barrier_journal(self) -> Path | None:
        raw = self.environment.get(_CHILD_BARRIER_JOURNAL_ENV)
        if raw is None:
            return None
        journal = Path(raw)
        if journal != self.child_barrier_journal_path:
            raise RuntimeError("child barrier journal does not belong to this authority")
        try:
            _validate_child_barrier_journal(journal)
        except FileNotFoundError:
            if self._allow_missing_recovery_paths:
                return None
            raise
        return journal

    def cleanup_child_barriers(self) -> None:
        """Validate and exhaust every child-created direct-/tmp barrier intent."""
        journal = self._child_barrier_journal()
        if journal is None:
            return
        expected_journal = _validate_child_barrier_journal(journal)
        descriptor = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened_journal = os.fstat(descriptor)
            if (
                opened_journal.st_dev != expected_journal.st_dev
                or opened_journal.st_ino != expected_journal.st_ino
            ):
                raise RuntimeError("child barrier journal identity changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                locked_journal = os.fstat(descriptor)
                maximum_size = 1024 * 1024
                expected_size = locked_journal.st_size
                if expected_size < 0 or expected_size > maximum_size:
                    raise RuntimeError("child barrier journal is too large")
                chunks: list[bytes] = []
                consumed = 0
                while consumed < expected_size:
                    chunk = os.read(
                        descriptor,
                        min(64 * 1024, expected_size - consumed),
                    )
                    if not chunk:
                        raise RuntimeError("child barrier journal read made no progress")
                    chunks.append(chunk)
                    consumed += len(chunk)
                    if consumed > maximum_size:
                        raise RuntimeError("child barrier journal is too large")
                extra = os.read(descriptor, 1)
                if extra:
                    raise RuntimeError("child barrier journal changed during bounded read")
                raw = b"".join(chunks)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        if raw and not raw.endswith(b"\n"):
            raise RuntimeError("child barrier journal has a partial record")

        records: dict[Path, str] = {}
        for line in raw.splitlines():
            try:
                decoded = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("child barrier journal is malformed") from error
            if not isinstance(decoded, dict) or set(decoded) != {"failpoint", "root"}:
                raise RuntimeError("child barrier journal record schema is invalid")
            failpoint = decoded["failpoint"]
            raw_root = decoded["root"]
            if (
                not isinstance(failpoint, str)
                or not failpoint
                or "/" in failpoint
                or failpoint in {".", ".."}
                or not isinstance(raw_root, str)
            ):
                raise RuntimeError("child barrier journal record identity is invalid")
            root = Path(raw_root)
            if (
                root.parent != Path("/tmp")
                or re.fullmatch(r"jhin-p10-barrier-[0-9a-f]{32}", root.name) is None
            ):
                raise RuntimeError("child barrier journal root is outside exact authority")
            previous = records.setdefault(root, failpoint)
            if previous != failpoint:
                raise RuntimeError("child barrier journal reused one root identity")

        errors: list[BaseException] = []
        for root, failpoint in records.items():
            try:
                try:
                    root_metadata = root.lstat()
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISLNK(root_metadata.st_mode)
                    or not stat.S_ISDIR(root_metadata.st_mode)
                    or root_metadata.st_uid != os.getuid()
                    or stat.S_IMODE(root_metadata.st_mode) not in {0o700, 0o711}
                ):
                    raise RuntimeError(f"child barrier root identity changed: {root}")
                entries = list(root.iterdir())
                if any(entry.name != failpoint for entry in entries) or len(entries) > 1:
                    raise RuntimeError(f"child barrier root has unexpected entries: {root}")
                if entries:
                    selected = entries[0]
                    selected_metadata = selected.lstat()
                    if (
                        stat.S_ISLNK(selected_metadata.st_mode)
                        or not stat.S_ISDIR(selected_metadata.st_mode)
                        or selected_metadata.st_uid != os.getuid()
                        or stat.S_IMODE(selected_metadata.st_mode) not in {0o700, 0o1777}
                    ):
                        raise RuntimeError(f"child barrier failpoint identity changed: {selected}")
                os.chmod(root, 0o700, follow_symlinks=False)
                shutil.rmtree(root)
                if root.exists() or root.is_symlink():
                    raise RuntimeError(f"child barrier survived exact cleanup: {root}")
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("failed to exhaust child barrier journal", errors)

    def remove_recovery_paths(self) -> None:
        """Remove journaled child barriers before their leased parent paths."""
        self.cleanup_child_barriers()
        self.remove_runtime_paths()

    def remove_runtime_paths(self) -> None:
        errors: list[BaseException] = []
        upgrade_paths = [
            Path(value)
            for key, value in self._environment_items
            if re.fullmatch(r"PHASE10_UPGRADE_BARRIER_(?:NORMAL|APPROVAL|SYNC|CLEANUP)_HOST", key)
        ]
        for path in (*upgrade_paths, self.barrier_root, self.runtime_dir):
            try:
                if not path.exists():
                    continue
                current = path.lstat()
                if path.parent != Path("/tmp") or not stat.S_ISDIR(current.st_mode):
                    raise RuntimeError(f"refusing unsafe runtime cleanup: {path}")
                os.chmod(path, 0o700, follow_symlinks=False)
                shutil.rmtree(path)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("failed to remove Phase 10 runtime paths", errors)

    def _run(
        self,
        command: tuple[str, ...],
        *,
        runner: CommandRunner,
        timeout: float,
        check: bool = True,
        input_bytes: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        self.assert_socket_unchanged()
        return runner(
            command,
            env=dict(self.environment if environment is None else environment),
            cwd=self.repo,
            timeout=timeout,
            check=check,
            input_bytes=input_bytes,
        )

    def worker_environment(
        self,
        *,
        barrier: BarrierRoot | None = None,
        identity: str | None = None,
    ) -> dict[str, str]:
        if identity is not None and re.fullmatch(r"[0-9a-f-]{36}", identity) is None:
            raise ValueError("worker barrier identity must be a lowercase UUID")
        environment = self.environment
        environment.update(
            {
                "APP_ENV": "test",
                "JHIN_TEST_CRASH_BARRIER_HOST_DIR": str(
                    self.barrier_root if barrier is None else barrier.root
                ),
                "JHIN_TEST_CRASH_BARRIER_DIR": (
                    "" if barrier is None else "/run/jhin/test-barriers"
                ),
                "JHIN_TEST_CRASH_BARRIER_NAME": ("" if barrier is None else barrier.failpoint),
                "JHIN_TEST_CRASH_BARRIER_MATCH": identity or "",
            }
        )
        return environment

    def worker_recreate_command(self, *services: str) -> tuple[str, ...]:
        if (
            not services
            or len(set(services)) != len(services)
            or any(service not in {"agent-worker", "tool-worker"} for service in services)
        ):
            raise ValueError("only distinct Phase 10 workers may be recreated")
        return self.compose_command(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--build",
            "--wait",
            "--wait-timeout",
            "300",
            *services,
        )

    def service_once_command(
        self,
        service: str,
        *,
        environment: Mapping[str, str],
    ) -> tuple[str, ...]:
        if service not in {"agent-worker", "tool-worker"}:
            raise ValueError("only a Phase 10 worker image may run this startup probe")
        forbidden = [
            key
            for key in environment
            if key
            not in {
                "APP_ENV",
                *[f"JHIN_TEST_CRASH_BARRIER_{part}" for part in ("DIR", "NAME", "MATCH")],
            }
        ]
        if forbidden or any("\n" in key or "\n" in value for key, value in environment.items()):
            raise ValueError("service-once environment contains an unsupported key or value")
        arguments = ["run", "--rm", "--no-deps"]
        for key, value in sorted(environment.items()):
            arguments.extend(("-e", f"{key}={value}"))
        arguments.append(service)
        return self.compose_command(*arguments)

    def run_service_once(
        self,
        service: str,
        *,
        environment: Mapping[str, str],
        timeout: float,
        runner: CommandRunner = run_command,
    ) -> subprocess.CompletedProcess[Any]:
        return self._run(
            self.service_once_command(service, environment=environment),
            runner=runner,
            timeout=timeout,
            check=False,
        )

    def counting_provider_command(
        self,
        *,
        image_id: str,
        marker: str,
    ) -> tuple[str, ...]:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise ValueError("counting provider requires an immutable image ID")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", marker) is None:
            raise ValueError("counting provider marker is invalid")
        name = f"{self.project}-model-counter"
        return self.docker_command(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"jhin.phase10.invocation={self.token}",
            "--label",
            f"jhin.phase10.counting-provider={name}",
            "--network",
            f"{self.project}_data",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--entrypoint",
            "python",
            image_id,
            "-c",
            _COUNTING_PROVIDER_SCRIPT,
            marker,
        )

    def start_counting_provider(
        self,
        *,
        marker: str,
        runner: CommandRunner = run_command,
    ) -> CountingProvider:
        fake_provider = self.inspect_service("fake-provider", runner=runner)
        image_id = fake_provider.get("Image")
        if not isinstance(image_id, str):
            raise RuntimeError("fake provider inspect omitted its immutable image ID")
        command = self.counting_provider_command(image_id=image_id, marker=marker)
        started = self._run(command, runner=runner, timeout=60.0)
        identifiers = [line.strip() for line in _text(started.stdout).splitlines() if line.strip()]
        if len(identifiers) != 1:
            raise RuntimeError("selected daemon returned an ambiguous counting-provider ID")
        provider = CountingProvider(
            authority=self,
            container_id=identifiers[0],
            name=f"{self.project}-model-counter",
            marker=marker,
        )
        health_script = (
            "import json,urllib.request;"
            "value=json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8080/__phase10_health',timeout=2));"
            "raise SystemExit(0 if value=={'ok':True} else 1)"
        )
        deadline = time.monotonic() + 30.0
        while True:
            health = self._run(
                self.docker_command("exec", provider.container_id, "python", "-c", health_script),
                runner=runner,
                timeout=10.0,
                check=False,
            )
            if health.returncode == 0:
                return provider
            if time.monotonic() >= deadline:
                try:
                    provider.close(runner=runner)
                finally:
                    raise TimeoutError("counting provider did not become ready")
            time.sleep(0.1)

    def _emit_worker_recovery_diagnostics(
        self,
        *,
        output: str,
        selected: Sequence[str],
        fixed_identities: Mapping[str, str],
        runner: CommandRunner,
        environment: Mapping[str, str],
    ) -> None:
        """Emit final bounded ps/log/inspect evidence without exposing configuration."""
        ps_command = self.compose_command("ps", "--all", "--format", "json")
        emit_live_failure_output(
            subprocess.CalledProcessError(1, ps_command, output=output, stderr=""),
            environment=dict(environment),
            context="stack-ps",
        )

        rows: dict[str, dict[str, Any]] = {}
        try:
            for row in _decode_compose_rows(output):
                service = row.get("Service")
                if isinstance(service, str) and service and service not in rows:
                    rows[service] = row
        except ComposePsError:
            rows = {}
        diagnostic_services = set(selected)
        for service, row in rows.items():
            state = str(row.get("State", "")).lower()
            health_value = row.get("Health")
            health = "" if health_value is None else str(health_value).lower()
            identifier = row.get("ID")
            if (
                state != "running"
                or health != "healthy"
                or (service in fixed_identities and identifier != fixed_identities[service])
            ):
                diagnostic_services.add(service)

        logs_command = self.compose_command(
            "logs",
            "--no-color",
            "--tail",
            "100",
            *sorted(diagnostic_services),
        )
        logs = self._run(
            logs_command,
            runner=runner,
            timeout=60.0,
            check=False,
            environment=environment,
        )
        emit_live_failure_output(
            subprocess.CalledProcessError(
                logs.returncode or 1,
                logs_command,
                output=logs.stdout,
                stderr=logs.stderr,
            ),
            environment=dict(environment),
            context="stack-logs",
        )

        operational: dict[str, Any] = {}
        for service in sorted(diagnostic_services):
            diagnostic_row = rows.get(service)
            identifier = None if diagnostic_row is None else diagnostic_row.get("ID")
            if not isinstance(identifier, str) or not identifier:
                operational[service] = {"error": "Compose ps omitted the container ID"}
                continue
            inspect_command = self.docker_command("inspect", identifier)
            inspected = self._run(
                inspect_command,
                runner=runner,
                timeout=30.0,
                check=False,
                environment=environment,
            )
            if inspected.returncode != 0:
                operational[service] = {
                    "container_id": identifier,
                    "inspect_returncode": inspected.returncode,
                }
                continue
            try:
                payload = json.loads(_text(inspected.stdout))
            except json.JSONDecodeError:
                payload = None
            if (
                not isinstance(payload, list)
                or len(payload) != 1
                or not isinstance(payload[0], dict)
                or payload[0].get("Id") != identifier
            ):
                operational[service] = {
                    "container_id": identifier,
                    "error": "Docker inspect output was malformed or changed identity",
                }
                continue
            operational[service] = _operational_container_fields(payload[0])
        rendered = _redact_live_failure_text(
            json.dumps(operational, sort_keys=True, default=str),
            dict(environment),
        )
        if rendered:
            sys.stderr.write("\n--- live operational-inspect (redacted tail) ---\n")
            sys.stderr.write(rendered)
            if not rendered.endswith("\n"):
                sys.stderr.write("\n")
        self.assert_socket_unchanged()

    def _raise_worker_recovery_failure(
        self,
        error: BaseException,
        *,
        output: str,
        selected: Sequence[str],
        fixed_identities: Mapping[str, str],
        runner: CommandRunner,
        environment: Mapping[str, str],
    ) -> None:
        try:
            self._emit_worker_recovery_diagnostics(
                output=output,
                selected=selected,
                fixed_identities=fixed_identities,
                runner=runner,
                environment=environment,
            )
        except BaseException as diagnostic_error:
            raise BaseExceptionGroup(
                "worker recovery and bounded diagnostics failed",
                [error, diagnostic_error],
            ) from error
        raise error

    def recreate_workers(
        self,
        services: Sequence[str],
        *,
        barrier: BarrierRoot | None = None,
        identity: str | None = None,
        runner: CommandRunner = run_command,
    ) -> None:
        selected = tuple(services)
        environment = self.worker_environment(barrier=barrier, identity=identity)
        baseline_result = self._run(
            self.compose_command("ps", "--all", "--format", "json"),
            runner=runner,
            timeout=60.0,
            environment=environment,
        )
        baseline = _parse_worker_recovery_inventory(
            _text(baseline_result.stdout),
            self.expected_services,
        )
        fixed_identities = {
            service: cast(str, row["ID"])
            for service, row in baseline.items()
            if service not in selected
        }
        baseline_selected_identities = {
            service: cast(str, baseline[service]["ID"]) for service in selected
        }
        try:
            self._run(
                self.worker_recreate_command(*selected),
                runner=runner,
                timeout=1200.0,
                environment=environment,
            )
        except subprocess.CalledProcessError as error:
            try:
                failed_inventory = self._run(
                    self.compose_command("ps", "--all", "--format", "json"),
                    runner=runner,
                    timeout=60.0,
                    check=False,
                    environment=environment,
                )
                if failed_inventory.returncode != 0:
                    raise subprocess.CalledProcessError(
                        failed_inventory.returncode,
                        failed_inventory.args,
                        output=failed_inventory.stdout,
                        stderr=failed_inventory.stderr,
                    )
            except BaseException as diagnostic_error:
                raise BaseExceptionGroup(
                    "worker recreation and fresh inventory diagnostics failed",
                    [error, diagnostic_error],
                ) from error
            self._raise_worker_recovery_failure(
                error,
                output=_text(failed_inventory.stdout),
                selected=selected,
                fixed_identities=fixed_identities,
                runner=runner,
                environment=environment,
            )
            raise AssertionError("worker recovery failure unexpectedly returned") from error
        deadline_started = time.monotonic()
        replacement_identities: dict[str, str] = {}
        while True:
            result = self._run(
                self.compose_command("ps", "--all", "--format", "json"),
                runner=runner,
                timeout=60.0,
                environment=environment,
            )
            output = _text(result.stdout)
            try:
                parsed, transitional = _classify_worker_recovery(
                    output,
                    expected_services=self.expected_services,
                    fixed_identities={**fixed_identities, **replacement_identities},
                )
                if not replacement_identities:
                    unchanged = sorted(
                        service
                        for service, baseline_identifier in baseline_selected_identities.items()
                        if parsed[service]["ID"] == baseline_identifier
                    )
                    if unchanged:
                        raise ComposePsError(f"selected workers were not recreated: {unchanged}")
            except ComposePsError as error:
                self._raise_worker_recovery_failure(
                    error,
                    output=output,
                    selected=selected,
                    fixed_identities={**fixed_identities, **replacement_identities},
                    runner=runner,
                    environment=environment,
                )
                raise AssertionError("worker recovery failure unexpectedly returned") from error
            if not replacement_identities:
                replacement_identities = {
                    service: cast(str, parsed[service]["ID"]) for service in selected
                }
            if not transitional:
                break
            elapsed = time.monotonic() - deadline_started
            if elapsed >= 90.0:
                timeout_error = TimeoutError(
                    "worker recovery remained transitional "
                    f"({', '.join(transitional)}) for {elapsed:.1f} seconds"
                )
                self._raise_worker_recovery_failure(
                    timeout_error,
                    output=output,
                    selected=selected,
                    fixed_identities={**fixed_identities, **replacement_identities},
                    runner=runner,
                    environment=environment,
                )
                raise AssertionError(
                    "worker recovery timeout unexpectedly returned"
                ) from timeout_error
            time.sleep(min(3.0, 90.0 - elapsed))
        self.assert_socket_unchanged()
        expected = {
            key: environment[key]
            for key in (
                "APP_ENV",
                "JHIN_TEST_CRASH_BARRIER_DIR",
                "JHIN_TEST_CRASH_BARRIER_NAME",
                "JHIN_TEST_CRASH_BARRIER_MATCH",
            )
        }
        for service in selected:
            inspected = self.inspect_service(service, runner=runner)
            values = inspected.get("Config", {}).get("Env", [])
            if not isinstance(values, list):
                raise RuntimeError("worker inspect omitted its environment")
            observed = dict(
                item.split("=", 1) for item in values if isinstance(item, str) and "=" in item
            )
            if any(observed.get(key) != value for key, value in expected.items()):
                raise RuntimeError("worker barrier environment differs from the selected identity")

    def recreate_worker(
        self,
        service: str,
        *,
        barrier: BarrierRoot | None = None,
        identity: str | None = None,
        runner: CommandRunner = run_command,
    ) -> None:
        self.recreate_workers(
            (service,),
            barrier=barrier,
            identity=identity,
            runner=runner,
        )

    def stop_service(
        self,
        service: str,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        if service not in {"agent-worker", "tool-worker"}:
            raise ValueError("only a Phase 10 worker may be stopped")
        self._run(
            self.compose_command("stop", "--timeout", "30", service),
            runner=runner,
            timeout=60.0,
        )

    def kill_service(
        self,
        service: str,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        inspected = self.inspect_service(service, runner=runner)
        identifier = inspected.get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("worker inspect did not return a container ID")
        self._run(
            self.docker_command("update", "--restart=no", identifier),
            runner=runner,
            timeout=30.0,
        )
        self._run(
            self.docker_command("kill", "--signal", "SIGKILL", identifier),
            runner=runner,
            timeout=30.0,
        )
        stopped = self._run(
            self.docker_command("inspect", identifier),
            runner=runner,
            timeout=30.0,
        )
        try:
            payload = json.loads(_text(stopped.stdout))
            running = payload[0]["State"]["Running"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise RuntimeError("worker stop inspect data is malformed") from error
        if running is not False:
            raise RuntimeError("SIGKILLed worker is still running")

    def install_master_key(self, *, runner: CommandRunner = run_command) -> None:
        """Create the key as in-container UID 10001 on the selected daemon."""
        if self.master_key_path.exists():
            raise RuntimeError("master key path already exists")
        os.chmod(self.runtime_dir, 0o733, follow_symlinks=False)
        script = (
            "import base64,os,secrets;"
            "path='/stage/jhin_master_key';"
            "data=base64.b64encode(secrets.token_bytes(32))+b'\\n';"
            "fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400);"
            "os.write(fd,data);os.fchmod(fd,0o400);os.fchown(fd,10001,10001);"
            "os.fsync(fd);os.close(fd);"
            "directory=os.open('/stage',os.O_RDONLY|os.O_DIRECTORY);"
            "os.fsync(directory);os.close(directory)"
        )
        name = f"{self.project}-phase10-key-install"
        command = self.docker_command(
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            f"jhin.phase10.invocation={self.token}",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,src={self.runtime_dir},dst=/stage",
            self.runner_image,
            "python",
            "-c",
            script,
        )
        try:
            self._run(command, runner=runner, timeout=60.0)
        finally:
            os.chmod(self.runtime_dir, 0o711, follow_symlinks=False)
        metadata = self.master_key_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or self.master_key_path.is_symlink():
            raise RuntimeError("selected daemon did not create a regular master key")
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            raise RuntimeError("selected daemon did not install the master key at mode 0400")

    def build_required_images(self, *, runner: CommandRunner = run_command) -> None:
        """Build the shared runner first, then the profile-gated job image."""
        self._run(
            self.compose_command("build", "sandbox-runner"),
            runner=runner,
            timeout=900.0,
        )
        self._run(
            self.compose_command("--profile", "build", "build", "sandbox-image"),
            runner=runner,
            timeout=900.0,
        )

    def assert_ready(
        self,
        *,
        runner: CommandRunner = run_command,
        expected_services: Iterable[str] | None = None,
        upgrade: bool = False,
    ) -> dict[str, dict[str, Any]]:
        expected = self.expected_services if expected_services is None else set(expected_services)
        if not expected:
            raise ValueError("readiness selection cannot be empty")
        if not upgrade and not expected <= self.expected_services:
            raise ValueError("readiness selection is outside the exact service inventory")
        service_selection = () if expected_services is None else tuple(sorted(expected))
        result = self._run(
            self.compose_command(
                "ps",
                "--all",
                "--format",
                "json",
                *service_selection,
                upgrade=upgrade,
            ),
            runner=runner,
            timeout=60.0,
        )
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="strict")
        return parse_compose_ps(cast(str, output), expected)

    def emit_stack_diagnostics(self, *, runner: CommandRunner = run_command) -> None:
        """Emit bounded, redacted selected-project state and logs."""
        diagnostics = (
            ("stack-ps", self.compose_command("ps", "--all", "--format", "json")),
            ("stack-logs", self.compose_command("logs", "--no-color", "--tail", "100")),
        )
        for context, command in diagnostics:
            result = self._run(
                command,
                runner=runner,
                timeout=60.0,
                check=False,
            )
            error = subprocess.CalledProcessError(
                result.returncode or 1,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
            emit_live_failure_output(
                error,
                environment=self.environment,
                context=context,
            )

    def wait_ready(
        self,
        *,
        runner: CommandRunner = run_command,
        expected_services: Iterable[str] | None = None,
        timeout_seconds: float = 90.0,
        interval_seconds: float = 3.0,
    ) -> dict[str, dict[str, Any]]:
        """Wait boundedly while an exact running service is still starting."""
        if timeout_seconds <= 0 or interval_seconds <= 0:
            raise ValueError("readiness timeout and interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                return self.assert_ready(
                    runner=runner,
                    expected_services=expected_services,
                )
            except ComposeStartingError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self.emit_stack_diagnostics(runner=runner)
                    except BaseException as diagnostic_error:
                        raise BaseExceptionGroup(
                            "Phase 10 readiness and diagnostics failed",
                            [error, diagnostic_error],
                        ) from error
                    raise
                time.sleep(min(interval_seconds, remaining))

    def inspect_service(
        self,
        service: str,
        *,
        runner: CommandRunner = run_command,
        upgrade: bool = False,
    ) -> dict[str, Any]:
        if service not in self.expected_services and not (
            upgrade
            and re.fullmatch(
                r"(?:phase9-agent-worker|phase10-agent-worker|phase10-tool-worker)-(?:normal|approval|sync|cleanup)",
                service,
            )
        ):
            raise ValueError(f"service is outside the exact Phase 10 inventory: {service}")
        profile = ("--profile", "phase10-upgrade") if upgrade else ()
        selected = self._run(
            self.compose_command(*profile, "ps", "-q", service, upgrade=upgrade),
            runner=runner,
            timeout=30.0,
        )
        identifiers = [line.strip() for line in _text(selected.stdout).splitlines() if line.strip()]
        if len(identifiers) != 1:
            raise RuntimeError(f"expected exactly one container for service {service}")
        inspected = self._run(
            self.docker_command("inspect", identifiers[0]),
            runner=runner,
            timeout=30.0,
        )
        try:
            payload = json.loads(_text(inspected.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Docker returned malformed inspect data for {service}") from error
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise RuntimeError(f"Docker returned ambiguous inspect data for {service}")
        if payload[0].get("Id") != identifiers[0]:
            raise RuntimeError(f"Docker inspect identity changed for {service}")
        return cast(dict[str, Any], payload[0])

    def _exec_service(
        self,
        service: str,
        *arguments: str,
        runner: CommandRunner = run_command,
    ) -> subprocess.CompletedProcess[Any]:
        return self._run(
            self.compose_command("exec", "-T", service, *arguments),
            runner=runner,
            timeout=30.0,
            check=False,
        )

    def service_http_json_command(self, service: str, url: str) -> tuple[str, ...]:
        allowed = {
            ("tool-worker", "http://sandbox-runner:8085/health"),
            ("sandbox-runner", "http://rootless-docker-transport:2375/version"),
        }
        if (service, url) not in allowed:
            raise ValueError("private HTTP probe is outside the exact service boundary")
        script = (
            "import json,sys,urllib.request;"
            "value=json.load(urllib.request.urlopen(sys.argv[1],timeout=3));"
            "print(json.dumps(value,sort_keys=True,separators=(',',':')))"
        )
        return self.compose_command("exec", "-T", service, "python", "-c", script, url)

    def service_http_json(
        self,
        service: str,
        url: str,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, Any]:
        result = self._run(
            self.service_http_json_command(service, url),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("private HTTP probe failed")
        try:
            payload = json.loads(_text(result.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("private HTTP probe returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("private HTTP probe returned malformed JSON")
        return cast(dict[str, Any], payload)

    def upgrade_agent_runner_probe_command(self, service: str) -> tuple[str, ...]:
        if re.fullmatch(r"phase10-agent-worker-(?:normal|approval|sync|cleanup)", service) is None:
            raise ValueError("runner isolation probe requires an exact Phase 10 agent service")
        script = "import sys,urllib.request;urllib.request.urlopen(sys.argv[1],timeout=3).read()"
        return self.compose_command(
            "--profile",
            "phase10-upgrade",
            "exec",
            "-T",
            service,
            "python",
            "-c",
            script,
            "http://sandbox-runner:8085/health",
            upgrade=True,
        )

    def upgrade_agent_runner_probe(
        self,
        service: str,
        *,
        runner: CommandRunner = run_command,
    ) -> int:
        result = self._run(
            self.upgrade_agent_runner_probe_command(service),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        return result.returncode

    def inspect_socket_from_adapter(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, Any]:
        if self.mode != "rootless":
            raise RuntimeError("adapter socket inspection requires rootless mode")
        script = (
            "import json,os,stat;value=os.stat('/run/host/docker.sock',follow_symlinks=False);"
            "print(json.dumps({'uid':value.st_uid,'gid':value.st_gid,"
            "'socket':stat.S_ISSOCK(value.st_mode)},sort_keys=True))"
        )
        result = self._exec_service(
            "rootless-docker-transport",
            "python",
            "-c",
            script,
            runner=runner,
        )
        if result.returncode != 0:
            raise RuntimeError("rootless adapter could not inspect its selected socket")
        try:
            payload = json.loads(_text(result.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("rootless adapter returned malformed socket metadata") from error
        if not isinstance(payload, dict):
            raise RuntimeError("rootless adapter returned malformed socket metadata")
        return cast(dict[str, Any], payload)

    def service_dns_probe(
        self,
        service: str,
        hostname: str,
        *,
        runner: CommandRunner = run_command,
    ) -> int:
        script = "import socket,sys;socket.getaddrinfo(sys.argv[1],2375)"
        result = self._exec_service(
            service,
            "python",
            "-c",
            script,
            hostname,
            runner=runner,
        )
        return result.returncode

    def adapter_ping_from_runner(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> bytes:
        if self.mode != "rootless":
            raise RuntimeError("adapter ping requires rootless mode")
        script = (
            "import urllib.request;"
            "print(urllib.request.urlopen('http://rootless-docker-transport:2375/_ping',"
            "timeout=3).read().decode())"
        )
        result = self._exec_service(
            "sandbox-runner",
            "python",
            "-c",
            script,
            runner=runner,
        )
        if result.returncode != 0:
            raise RuntimeError("sandbox runner could not reach the private rootless adapter")
        return _text(result.stdout).strip().encode()

    @classmethod
    def noop_sandbox_job_request(
        cls,
        job_id: str,
        *,
        workspace_key: str = "",
        require_existing_workspace: bool = False,
    ) -> dict[str, Any]:
        cls.sandbox_job_label(job_id)
        if workspace_key:
            cls.sandbox_workspace_key(workspace_key)
        elif require_existing_workspace:
            raise ValueError("workspace reuse requires an exact workspace key")
        verification = (
            "import os\n"
            "from pathlib import Path\n"
            "installed = Path('/etc/inputrc')\n"
            "canonical = Path('/usr/share/readline/inputrc')\n"
            "assert installed.read_bytes() == canonical.read_bytes()\n"
            "metadata = os.stat(installed)\n"
            "assert (metadata.st_uid, metadata.st_gid, metadata.st_mode & 0o777) == "
            "(0, 0, 0o644)\n"
        )
        if workspace_key:
            verification += "marker = Path('/workspace/.phase10-volume-reuse')\n"
            if require_existing_workspace:
                verification += "assert marker.read_bytes() == b'phase10-volume-reuse\\n'\n"
            else:
                verification += "marker.write_bytes(b'phase10-volume-reuse\\n')\n"
        verification += "print('phase10-noop')\n"
        request = {
            "job_id": job_id,
            "command": ["python3", "-c", verification],
            "network_policy": "none",
        }
        if workspace_key:
            request["workspace_key"] = workspace_key
        return request

    def run_noop_sandbox_job(
        self,
        *,
        timeout: float = 30.0,
        workspace_key: str = "",
        require_existing_workspace: bool = False,
    ) -> dict[str, Any]:
        port = self.published_ports.get("SANDBOX_RUNNER_DEV_PORT")
        if port is None:
            raise RuntimeError("sandbox runner host endpoint was not resolved")
        job_id = secrets.token_hex(12)
        self.record_direct_sandbox_job(job_id)
        if workspace_key:
            self.record_direct_sandbox_workspace(workspace_key)
        endpoint = f"http://127.0.0.1:{port}"
        headers = {
            "Authorization": f"Bearer {self.environment['SANDBOX_RUNNER_TOKEN']}",
            "Content-Type": "application/json",
        }
        body = json.dumps(
            self.noop_sandbox_job_request(
                job_id,
                workspace_key=workspace_key,
                require_existing_workspace=require_existing_workspace,
            )
        ).encode()
        request = urllib.request.Request(
            f"{endpoint}/v1/jobs",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            if response.status != 202:
                raise RuntimeError("sandbox runner rejected the no-op job")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status_request = urllib.request.Request(
                f"{endpoint}/v1/jobs/{job_id}",
                headers=headers,
            )
            with urllib.request.urlopen(status_request, timeout=5.0) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise RuntimeError("sandbox runner returned malformed job status")
            if payload.get("status") in {"completed", "failed", "timeout", "cancelled"}:
                return cast(dict[str, Any], payload)
            time.sleep(0.1)
        raise TimeoutError("sandbox runner no-op job did not finish")

    def delete_sandbox_workspace(
        self,
        workspace_key: str,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        self.sandbox_workspace_key(workspace_key)
        port = self.published_ports.get("SANDBOX_RUNNER_DEV_PORT")
        if port is None:
            raise RuntimeError("sandbox runner host endpoint was not resolved")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/workspaces/{workspace_key}",
            headers={"Authorization": f"Bearer {self.environment['SANDBOX_RUNNER_TOKEN']}"},
            method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            if response.status != 204:
                raise RuntimeError("sandbox runner rejected workspace deletion")
        if self._exact_label_ids(
            runner=runner,
            resource="volume",
            label=f"jhin.sandbox.workspace={workspace_key}",
        ):
            raise RuntimeError("sandbox workspace volume survived deletion")

    @staticmethod
    def sandbox_job_label(job_id: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", job_id) is None:
            raise ValueError("sandbox job identity is malformed")
        return f"jhin.sandbox.job={job_id}"

    @staticmethod
    def sandbox_workspace_key(workspace_key: str) -> str:
        if _WORKSPACE_KEY.fullmatch(workspace_key) is None:
            raise ValueError("sandbox workspace identity is malformed")
        return workspace_key

    @property
    def direct_sandbox_job_ledger(self) -> Path:
        return self.runtime_dir / "direct-sandbox-jobs.json"

    @property
    def direct_sandbox_workspace_ledger(self) -> Path:
        return self.runtime_dir / "direct-sandbox-workspaces.json"

    def direct_sandbox_jobs(self) -> tuple[str, ...]:
        """Read the private crash-safe inventory of direct runner requests."""
        path = self.direct_sandbox_job_ledger
        if not path.exists() and not path.is_symlink():
            return ()
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise RuntimeError("direct sandbox job ledger metadata is unsafe")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                payload = json.load(stream)
        except json.JSONDecodeError as error:
            raise RuntimeError("direct sandbox job ledger is malformed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(payload, list)
            or any(not isinstance(value, str) for value in payload)
            or len(payload) != len(set(payload))
        ):
            raise RuntimeError("direct sandbox job ledger is malformed")
        for job_id in payload:
            self.sandbox_job_label(job_id)
        return tuple(cast(list[str], payload))

    def record_direct_sandbox_job(self, job_id: str) -> None:
        """Fsync an exact direct job ID before the runner can create resources."""
        self.sandbox_job_label(job_id)
        jobs = self.direct_sandbox_jobs()
        if job_id in jobs:
            raise RuntimeError("direct sandbox job identity was already recorded")
        path = self.direct_sandbox_job_ledger
        temporary = self.runtime_dir / f".direct-sandbox-jobs-{secrets.token_hex(6)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            encoded = json.dumps([*jobs, job_id], separators=(",", ":")).encode()
            _write_all(descriptor, encoded, description="direct sandbox job ledger")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            directory = os.open(
                self.runtime_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def direct_sandbox_workspaces(self) -> tuple[str, ...]:
        """Read the private crash-safe inventory of direct workspace keys."""
        path = self.direct_sandbox_workspace_ledger
        if not path.exists() and not path.is_symlink():
            return ()
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise RuntimeError("direct sandbox workspace ledger metadata is unsafe")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                payload = json.load(stream)
        except json.JSONDecodeError as error:
            raise RuntimeError("direct sandbox workspace ledger is malformed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(payload, list)
            or any(not isinstance(value, str) for value in payload)
            or len(payload) != len(set(payload))
        ):
            raise RuntimeError("direct sandbox workspace ledger is malformed")
        for workspace_key in payload:
            self.sandbox_workspace_key(workspace_key)
        return tuple(cast(list[str], payload))

    def record_direct_sandbox_workspace(self, workspace_key: str) -> None:
        """Fsync an exact workspace key before the runner can create its volume."""
        self.sandbox_workspace_key(workspace_key)
        workspaces = self.direct_sandbox_workspaces()
        if workspace_key in workspaces:
            return
        path = self.direct_sandbox_workspace_ledger
        temporary = self.runtime_dir / f".direct-sandbox-workspaces-{secrets.token_hex(6)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            encoded = json.dumps([*workspaces, workspace_key], separators=(",", ":")).encode()
            _write_all(descriptor, encoded, description="direct sandbox workspace ledger")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            directory = os.open(
                self.runtime_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def direct_sandbox_artifact_filters(
        self,
    ) -> tuple[tuple[Literal["container", "volume"], str], ...]:
        filters: list[tuple[Literal["container", "volume"], str]] = []
        for job_id in self.direct_sandbox_jobs():
            filters.append(("container", self.sandbox_job_label(job_id)))
        for workspace_key in self.direct_sandbox_workspaces():
            filters.append(("container", f"jhin.sandbox.workspace.init={workspace_key}"))
            filters.append(("volume", f"jhin.sandbox.workspace={workspace_key}"))
        return tuple(filters)

    @classmethod
    def blocking_sandbox_job_request(cls, job_id: str) -> dict[str, Any]:
        cls.sandbox_job_label(job_id)
        return {
            "job_id": job_id,
            "command": ["python3", "-c", "import time;time.sleep(300)"],
            "network_policy": "none",
            "timeout_seconds": 300,
        }

    def _sandbox_endpoint(self) -> tuple[str, dict[str, str]]:
        port = self.published_ports.get("SANDBOX_RUNNER_DEV_PORT")
        if port is None:
            raise RuntimeError("sandbox runner host endpoint was not resolved")
        return (
            f"http://127.0.0.1:{port}",
            {
                "Authorization": f"Bearer {self.environment['SANDBOX_RUNNER_TOKEN']}",
                "Content-Type": "application/json",
            },
        )

    def _sandbox_startup_diagnostics(
        self,
        *,
        job_id: str,
        endpoint: str,
        headers: dict[str, str],
        observed_ids: Sequence[str],
        container: Mapping[str, Any] | None,
    ) -> str:
        evidence: dict[str, Any] = {"observed_container_ids": list(observed_ids)}
        for key, suffix in (("runner_status", ""), ("runner_logs", "/logs")):
            request = urllib.request.Request(
                f"{endpoint}/v1/jobs/{job_id}{suffix}",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise RuntimeError(f"sandbox runner returned malformed {key} diagnostics")
            evidence[key] = payload
        evidence["container"] = (
            None if container is None else _operational_container_fields(container)
        )
        return _redact_live_failure_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            self.environment,
        )

    def _sandbox_startup_error(
        self,
        error: RuntimeError | TimeoutError,
        *,
        job_id: str,
        endpoint: str,
        headers: dict[str, str],
        observed_ids: Sequence[str],
        container: Mapping[str, Any] | None,
    ) -> RuntimeError | TimeoutError:
        diagnostics = self._sandbox_startup_diagnostics(
            job_id=job_id,
            endpoint=endpoint,
            headers=headers,
            observed_ids=observed_ids,
            container=container,
        )
        return type(error)(f"{error}; diagnostics={diagnostics}")

    def start_inspectable_sandbox_job(
        self,
        *,
        runner: CommandRunner = run_command,
        timeout: float = 30.0,
    ) -> RunningSandboxJob:
        job_id = secrets.token_hex(12)
        self.record_direct_sandbox_job(job_id)
        endpoint, headers = self._sandbox_endpoint()
        request = urllib.request.Request(
            f"{endpoint}/v1/jobs",
            data=json.dumps(self.blocking_sandbox_job_request(job_id)).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            if response.status != 202:
                raise RuntimeError("sandbox runner rejected the inspectable job")
        deadline = time.monotonic() + timeout
        container_id: str | None = None
        last_container: dict[str, Any] | None = None
        last_identifiers: list[str] = []
        try:
            while time.monotonic() < deadline:
                identifiers = self._exact_label_ids(
                    runner=runner,
                    resource="container",
                    label=self.sandbox_job_label(job_id),
                )
                last_identifiers = identifiers
                if container_id is None:
                    if len(identifiers) > 1:
                        raise self._sandbox_startup_error(
                            RuntimeError("sandbox job label resolved multiple containers"),
                            job_id=job_id,
                            endpoint=endpoint,
                            headers=headers,
                            observed_ids=identifiers,
                            container=None,
                        )
                    if identifiers:
                        container_id = identifiers[0]
                elif identifiers != [container_id]:
                    raise self._sandbox_startup_error(
                        RuntimeError(
                            "sandbox job identity changed after publication: "
                            f"expected {container_id}, observed {identifiers}"
                        ),
                        job_id=job_id,
                        endpoint=endpoint,
                        headers=headers,
                        observed_ids=identifiers,
                        container=last_container,
                    )
                if identifiers:
                    inspected = self._run(
                        self.docker_command("inspect", cast(str, container_id)),
                        runner=runner,
                        timeout=30.0,
                    )
                    try:
                        payload = json.loads(_text(inspected.stdout))
                    except json.JSONDecodeError as error:
                        raise RuntimeError("sandbox job inspect returned malformed JSON") from error
                    if (
                        not isinstance(payload, list)
                        or len(payload) != 1
                        or not isinstance(payload[0], dict)
                    ):
                        raise RuntimeError("sandbox job inspect returned malformed identity data")
                    last_container = cast(dict[str, Any], payload[0])
                    labels = last_container.get("Config", {}).get("Labels", {})
                    state = last_container.get("State")
                    if (
                        last_container.get("Id") != container_id
                        or not isinstance(labels, dict)
                        or labels.get("jhin.sandbox.job") != job_id
                        or not isinstance(state, dict)
                    ):
                        raise self._sandbox_startup_error(
                            RuntimeError("sandbox job inspect identity changed or is malformed"),
                            job_id=job_id,
                            endpoint=endpoint,
                            headers=headers,
                            observed_ids=identifiers,
                            container=last_container,
                        )
                    status = state.get("Status")
                    running = state.get("Running")
                    if status == "running" and running is True:
                        return RunningSandboxJob(
                            job_id=job_id,
                            container_id=cast(str, container_id),
                            container=last_container,
                        )
                    if status == "created" and running is False:
                        time.sleep(0.05)
                        continue
                    raise self._sandbox_startup_error(
                        RuntimeError(
                            "terminal sandbox job state before security inspection: "
                            f"status={status!r}, running={running!r}"
                        ),
                        job_id=job_id,
                        endpoint=endpoint,
                        headers=headers,
                        observed_ids=identifiers,
                        container=last_container,
                    )
                time.sleep(0.05)
            detail = (
                "inspectable sandbox job remained created until its startup deadline"
                if last_container is not None
                and last_container.get("State", {}).get("Status") == "created"
                else "inspectable sandbox job identity was not published before its deadline"
            )
            raise self._sandbox_startup_error(
                TimeoutError(detail),
                job_id=job_id,
                endpoint=endpoint,
                headers=headers,
                observed_ids=last_identifiers,
                container=last_container,
            )
        except BaseException as error:
            try:
                self.cancel_sandbox_job(job_id, runner=runner, timeout=30.0)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "sandbox startup failed and exact cleanup also failed",
                    [error, cleanup_error],
                ) from error
            raise

    def cancel_sandbox_job(
        self,
        job: RunningSandboxJob | str,
        *,
        runner: CommandRunner = run_command,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        job_id = job.job_id if isinstance(job, RunningSandboxJob) else job
        label = self.sandbox_job_label(job_id)
        endpoint, headers = self._sandbox_endpoint()
        request = urllib.request.Request(
            f"{endpoint}/v1/jobs/{job_id}/cancel",
            data=b"",
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise RuntimeError("sandbox runner returned malformed cancellation status")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._exact_label_ids(
                runner=runner,
                resource="container",
                label=label,
            ):
                return cast(dict[str, Any], payload)
            time.sleep(0.05)
        raise TimeoutError("cancelled sandbox job container survived")

    def run_wrong_gid_probe(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> subprocess.CompletedProcess[Any]:
        if self.mode != "rootful" or self.socket_gid is None:
            raise RuntimeError("wrong-GID probe requires a rootful authority")
        self.preflight(runner=runner)
        wrong_gid = 1 if self.socket_gid != 1 else 2
        failure = self.with_rootful_socket_gid(wrong_gid)
        result: subprocess.CompletedProcess[Any] | None = None
        try:
            failure._run(
                failure.compose_command("build", "sandbox-runner"),
                runner=runner,
                timeout=900.0,
            )
            result = failure._run(
                failure.compose_command(
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--build",
                    "--wait",
                    "--wait-timeout",
                    "60",
                    "sandbox-runner",
                ),
                runner=runner,
                timeout=180.0,
                check=False,
            )
            if result.returncode == 0:
                raise RuntimeError("sandbox runner unexpectedly accepted an incorrect socket GID")
            return result
        finally:
            failure._run(
                failure.compose_command("rm", "-s", "-f", "sandbox-runner"),
                runner=runner,
                timeout=60.0,
                check=False,
            )
            self.assert_socket_unchanged()

    def resolve_published_ports(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, int]:
        resolved: dict[str, int] = {}
        for variable, service, container_port in PUBLISHED_ENDPOINTS:
            result = self._run(
                self.compose_command("port", service, str(container_port)),
                runner=runner,
                timeout=30.0,
            )
            output = result.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="strict")
            resolved[variable] = parse_compose_port(cast(str, output))
        if len(set(resolved.values())) != len(PUBLISHED_ENDPOINTS):
            raise RuntimeError("Docker allocated colliding Phase 10 host ports")
        return resolved

    def preflight(self, *, runner: CommandRunner = run_command) -> SocketMetadata:
        """Validate the immutable socket/daemon and refuse shared Jhin state."""
        executable = Path(self.docker_executable)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("Docker executable is unavailable")
        metadata = validate_socket_metadata(
            SocketMetadata.capture(self.socket_path),
            mode=self.mode,
            expected_gid=self.socket_gid,
        )
        if self.socket_snapshot is not None and metadata != self.socket_snapshot:
            raise RuntimeError("Docker socket metadata changed since authority selection")
        result = self._run(
            self.docker_command("info", "--format", "{{json .}}"),
            runner=runner,
            timeout=30.0,
        )
        try:
            info = json.loads(_text(result.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("selected Docker daemon returned malformed info") from error
        if not isinstance(info, dict):
            raise RuntimeError("selected Docker daemon returned malformed info")
        validate_daemon_info(info, mode=self.mode)
        self.assert_no_preexisting_shared_resources(runner=runner)
        return metadata

    def assert_socket_unchanged(self) -> None:
        if self.socket_snapshot is None:
            return
        current = SocketMetadata.capture(self.socket_path)
        if current != self.socket_snapshot:
            raise RuntimeError("selected Docker socket metadata changed during acceptance")

    def master_key_readability_command(
        self,
        service: str,
        *,
        upgrade: bool = False,
    ) -> tuple[str, ...]:
        if upgrade:
            if (
                re.fullmatch(
                    r"phase(?:9-agent|10-(?:agent|tool))-worker-"
                    r"(?:normal|approval|sync|cleanup)",
                    service,
                )
                is None
            ):
                raise ValueError("upgrade key probe service is outside the exact inventory")
        elif service not in {"api", "agent-worker", "tool-worker"}:
            raise ValueError("key probe service is outside the exact inventory")
        script = (
            "import base64,json,os,stat;"
            "path='/run/secrets/jhin_master_key';data=open(path,'rb').read().strip();"
            "metadata=os.stat(path,follow_symlinks=False);"
            "print(json.dumps({'uid':os.geteuid(),'owner':metadata.st_uid,"
            "'mode':stat.S_IMODE(metadata.st_mode),"
            "'decoded':len(base64.b64decode(data,validate=True))},sort_keys=True))"
        )
        profile = ("--profile", "phase10-upgrade") if upgrade else ()
        return self.compose_command(
            *profile,
            "exec",
            "-T",
            service,
            "python",
            "-c",
            script,
            upgrade=upgrade,
        )

    def verify_master_key_readability(
        self,
        *,
        runner: CommandRunner = run_command,
        services: Iterable[str] = ("api", "agent-worker", "tool-worker"),
        upgrade: bool = False,
    ) -> None:
        """Check only UID/owner/mode/decoded length; never print key bytes."""
        expected = {"decoded": 32, "mode": 0o400, "owner": 10001, "uid": 10001}
        for service in services:
            result = self._run(
                self.master_key_readability_command(service, upgrade=upgrade),
                runner=runner,
                timeout=30.0,
            )
            try:
                observed = json.loads(_text(result.stdout))
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{service} returned malformed key metadata") from error
            if observed != expected:
                raise RuntimeError(f"{service} cannot read the exact UID-10001 master key")

    def probe_rootless_capabilities(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        """Prove private-namespace port 80 and delegated cgroup-v2 limits."""
        if self.mode != "rootless":
            raise RuntimeError("rootless capability probes require rootless mode")
        common = (
            "run",
            "--rm",
            "--label",
            f"jhin.phase10.invocation={self.token}",
            "--network",
            "none",
        )
        port_script = (
            "import socket;server=socket.socket();server.bind(('0.0.0.0',80));server.close()"
        )
        self._run(
            self.docker_command(
                *common,
                "--name",
                f"{self.project}-rootless-port",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                self.runner_image,
                "python",
                "-c",
                port_script,
            ),
            runner=runner,
            timeout=60.0,
        )
        cgroup_script = (
            "import json,pathlib;root=pathlib.Path('/sys/fs/cgroup');"
            "print(json.dumps({'memory':(root/'memory.max').read_text().strip(),"
            "'cpu':(root/'cpu.max').read_text().strip(),"
            "'pids':(root/'pids.max').read_text().strip()},sort_keys=True))"
        )
        result = self._run(
            self.docker_command(
                *common,
                "--name",
                f"{self.project}-rootless-cgroup",
                "--memory",
                "64m",
                "--cpus",
                "0.25",
                "--pids-limit",
                "32",
                self.runner_image,
                "python",
                "-c",
                cgroup_script,
            ),
            runner=runner,
            timeout=60.0,
        )
        try:
            observed = json.loads(_text(result.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError("rootless cgroup probe returned malformed output") from error
        if observed != {"memory": "67108864", "cpu": "25000 100000", "pids": "32"}:
            raise RuntimeError("rootless daemon did not enforce exact cpu/memory/pids limits")

    def start_stack(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, int]:
        """Start one fresh bounded stack and return all Docker-assigned ports."""
        self.preflight(runner=runner)
        self.build_required_images(runner=runner)
        if self.mode == "rootless":
            self.probe_rootless_capabilities(runner=runner)
        self.install_master_key(runner=runner)
        self._run(
            self.compose_command("up", "-d", "--build", "--wait", "--wait-timeout", "300"),
            runner=runner,
            timeout=1200.0,
        )
        if self.mode == "rootless":
            self.wait_ready(
                runner=runner,
                expected_services={"rootless-docker-transport"},
            )
        self.wait_ready(runner=runner, expected_services={"sandbox-runner"})
        self.wait_ready(runner=runner)
        self._run(
            self.compose_command("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
            runner=runner,
            timeout=300.0,
        )
        self.wait_ready(runner=runner)
        self.verify_master_key_readability(runner=runner)
        return self.resolve_published_ports(runner=runner)

    def assert_no_preexisting_shared_resources(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> None:
        """Refuse a daemon where runner startup could reap another deployment."""
        probes = (
            self.docker_command("ps", "-aq", "--filter", "label=com.docker.compose.project=jhin"),
            self.docker_command(
                "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=jhin"
            ),
            self.docker_command(
                "network", "ls", "-q", "--filter", "label=com.docker.compose.project=jhin"
            ),
            self.docker_command("ps", "-aq", "--filter", "label=jhin.sandbox.job"),
            self.docker_command("ps", "-aq", "--filter", "label=jhin.sandbox.workspace.init"),
            self.docker_command("volume", "ls", "-q", "--filter", "label=jhin.sandbox.workspace"),
        )
        occupied: list[str] = []
        for command in probes:
            result = self._run(command, runner=runner, timeout=30.0, check=False)
            if result.returncode != 0:
                raise RuntimeError("failed to inspect pre-existing Docker authority")
            identifiers = [line for line in _text(result.stdout).splitlines() if line.strip()]
            if identifiers:
                occupied.append(command[-1])
        network = self._run(
            self.docker_command("network", "inspect", "jhin_sandbox"),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if network.returncode == 0:
            occupied.append("jhin_sandbox")
        elif network.returncode != 1:
            raise RuntimeError("failed to inspect the ordinary sandbox network")
        if occupied:
            raise RuntimeError(
                "selected daemon contains pre-existing ordinary Jhin or sandbox resources"
            )

    def _exact_label_ids(
        self,
        *,
        runner: CommandRunner,
        resource: Literal["container", "volume", "network"],
        label: str,
    ) -> list[str]:
        if resource == "container":
            command = self.docker_command("ps", "-aq", "--no-trunc", "--filter", f"label={label}")
        elif resource == "volume":
            command = self.docker_command("volume", "ls", "-q", "--filter", f"label={label}")
        else:
            command = self.docker_command("network", "ls", "-q", "--filter", f"label={label}")
        result = self._run(command, runner=runner, timeout=30.0, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to list exact-label {resource} resources")
        identifiers = [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]
        if resource == "container" and (
            any(_DOCKER_CONTAINER_ID.fullmatch(identifier) is None for identifier in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise RuntimeError("exact-label query returned noncanonical container IDs")
        return identifiers

    def snapshot_sandbox_artifacts(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> tuple[SandboxArtifact, ...]:
        """Record only job/run IDs owned by this isolated project's database."""
        postgres = self._run(
            self.compose_command("ps", "-q", "postgres"),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if postgres.returncode != 0:
            raise RuntimeError("failed to identify the isolated PostgreSQL container")
        containers = [line.strip() for line in _text(postgres.stdout).splitlines() if line.strip()]
        if not containers:
            return ()
        if len(containers) != 1:
            raise RuntimeError("isolated PostgreSQL identity is ambiguous")
        schema_probe = self._run(
            self.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-c",
                "SELECT CASE WHEN to_regclass('public.sandbox_job') IS NULL "
                "THEN 'missing' ELSE 'present' END",
            ),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if schema_probe.returncode != 0:
            raise RuntimeError("failed to inspect the isolated sandbox schema")
        schema_state = _text(schema_probe.stdout).strip()
        if schema_state == "missing":
            return ()
        if schema_state != "present":
            raise RuntimeError("isolated sandbox schema inventory is malformed")
        query = self._run(
            self.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-F",
                "|",
                "-c",
                "SELECT id::text, run_id::text FROM sandbox_job ORDER BY id",
            ),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if query.returncode != 0:
            raise RuntimeError("failed to snapshot isolated sandbox artifacts")
        artifacts: list[SandboxArtifact] = []
        for line in _text(query.stdout).splitlines():
            if not line.strip():
                continue
            fields = line.split("|")
            if len(fields) != 2 or any(_UUID.fullmatch(field) is None for field in fields):
                raise RuntimeError("isolated sandbox artifact inventory is malformed")
            artifacts.append(SandboxArtifact(job_id=fields[0], run_id=fields[1]))
        return tuple(artifacts)

    def temporal_run_id(
        self,
        domain_run_id: str,
        *,
        runner: CommandRunner = run_command,
    ) -> str:
        """Load the exact Temporal execution ID persisted for one agent run."""
        if _UUID.fullmatch(domain_run_id) is None:
            raise ValueError("domain run ID is malformed")
        result = self._run(
            self.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-c",
                (f"SELECT temporal_run_id FROM agent_run WHERE id = '{domain_run_id}'"),
            ),
            runner=runner,
            timeout=30.0,
        )
        rows = [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]
        if len(rows) != 1 or _UUID.fullmatch(rows[0]) is None:
            raise RuntimeError("agent run has no unique persisted Temporal run ID")
        return rows[0]

    def run_event_payload(
        self,
        domain_run_id: str,
        *,
        event_type: str,
        step: int,
        runner: CommandRunner = run_command,
    ) -> dict[str, Any]:
        """Load one unprojected DB event for an exact run/type/step identity."""
        if (
            _UUID.fullmatch(domain_run_id) is None
            or re.fullmatch(r"[a-z][a-z0-9_.]{2,80}", event_type) is None
            or type(step) is not int
            or not 0 <= step <= 128
        ):
            raise ValueError("run event lookup identity is malformed")
        statement = (
            "SELECT payload_json::text FROM run_event "
            f"WHERE run_id = '{domain_run_id}' "
            f"AND event_type = '{event_type}' "
            f"AND payload_json ->> 'step' = '{step}' ORDER BY created_at"
        )
        result = self._run(
            self.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-c",
                statement,
            ),
            runner=runner,
            timeout=30.0,
        )
        rows = [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]
        if len(rows) != 1:
            raise RuntimeError("run event lookup did not resolve exactly one row")
        try:
            payload = json.loads(rows[0])
        except json.JSONDecodeError as error:
            raise RuntimeError("run event payload is malformed") from error
        if not isinstance(payload, dict) or payload.get("step") != step:
            raise RuntimeError("run event payload does not match the exact step")
        return cast(dict[str, Any], payload)

    def sandbox_artifact_filters(
        self,
        artifacts: Iterable[SandboxArtifact],
    ) -> tuple[tuple[Literal["container", "volume"], str], ...]:
        """Derive exact Docker label filters from the authoritative DB inventory."""
        rows = tuple(artifacts)
        if any(
            _UUID.fullmatch(row.job_id) is None or _UUID.fullmatch(row.run_id) is None
            for row in rows
        ):
            raise ValueError("sandbox artifact identity is malformed")
        filters: list[tuple[Literal["container", "volume"], str]] = []
        filters.extend(
            ("container", f"jhin.sandbox.job={job_id}")
            for job_id in sorted({row.job_id for row in rows})
        )
        filters.extend(
            ("volume", f"jhin.sandbox.workspace=run-{run_id}")
            for run_id in sorted({row.run_id for row in rows})
        )
        return tuple(filters)

    def _remove_exact_containers(
        self,
        container_ids: Iterable[str],
        *,
        expected_label: tuple[str, str],
        runner: CommandRunner,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        label_name, label_value = expected_label
        for container_id in container_ids:
            try:
                inspected = self._run(
                    self.docker_command(
                        "inspect", "--format", "{{json .Config.Labels}}", container_id
                    ),
                    runner=runner,
                    timeout=30.0,
                )
                labels = json.loads(_text(inspected.stdout))
                if not isinstance(labels, dict) or labels.get(label_name) != label_value:
                    raise RuntimeError(
                        f"refusing to remove container without {label_name}={label_value}"
                    )
                removed = self._run(
                    self.docker_command("rm", "-f", container_id),
                    runner=runner,
                    timeout=60.0,
                    check=False,
                )
                if removed.returncode != 0:
                    raise RuntimeError(f"failed to remove exact container {container_id}")
            except BaseException as error:
                errors.append(error)
        return errors

    def _remove_exact_volumes(
        self,
        volume_ids: Iterable[str],
        *,
        expected_label: tuple[str, str],
        runner: CommandRunner,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        label_name, label_value = expected_label
        for volume_id in volume_ids:
            try:
                inspected = self._run(
                    self.docker_command(
                        "volume", "inspect", "--format", "{{json .Labels}}", volume_id
                    ),
                    runner=runner,
                    timeout=30.0,
                )
                labels = json.loads(_text(inspected.stdout))
                if not isinstance(labels, dict) or labels.get(label_name) != label_value:
                    raise RuntimeError(
                        f"refusing to remove volume without {label_name}={label_value}"
                    )
                removed = self._run(
                    self.docker_command("volume", "rm", volume_id),
                    runner=runner,
                    timeout=60.0,
                    check=False,
                )
                if removed.returncode != 0:
                    raise RuntimeError(f"failed to remove exact volume {volume_id}")
            except BaseException as error:
                errors.append(error)
        return errors

    def _remove_exact_networks(
        self,
        network_ids: Iterable[str],
        *,
        expected_label: tuple[str, str],
        runner: CommandRunner,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        label_name, label_value = expected_label
        for network_id in network_ids:
            try:
                inspected = self._run(
                    self.docker_command(
                        "network", "inspect", "--format", "{{json .Labels}}", network_id
                    ),
                    runner=runner,
                    timeout=30.0,
                )
                labels = json.loads(_text(inspected.stdout))
                if not isinstance(labels, dict) or labels.get(label_name) != label_value:
                    raise RuntimeError(
                        f"refusing to remove network without {label_name}={label_value}"
                    )
                removed = self._run(
                    self.docker_command("network", "rm", network_id),
                    runner=runner,
                    timeout=60.0,
                    check=False,
                )
                if removed.returncode != 0:
                    raise RuntimeError(f"failed to remove exact network {network_id}")
            except BaseException as error:
                errors.append(error)
        return errors

    def _exhaust_exact_labeled_resources(
        self,
        *,
        resource: Literal["container", "volume", "network"],
        label: str,
        description: str,
        runner: CommandRunner,
    ) -> list[BaseException]:
        """Use two cleanup passes and a separate final zero-survivor proof."""
        errors: list[BaseException] = []
        label_name, separator, label_value = label.partition("=")
        if not separator or not label_name or not label_value:
            return [ValueError("exact cleanup label is malformed")]
        for attempt in ("initial", "recovery"):
            try:
                identifiers = self._exact_label_ids(
                    runner=runner,
                    resource=resource,
                    label=label,
                )
                if not identifiers:
                    continue
                errors.append(
                    RuntimeError(
                        f"cleanup invariant: {len(identifiers)} {description} survived "
                        f"the {attempt} probe"
                    )
                )
                expected_label = (label_name, label_value)
                if resource == "container":
                    errors.extend(
                        self._remove_exact_containers(
                            identifiers,
                            expected_label=expected_label,
                            runner=runner,
                        )
                    )
                elif resource == "volume":
                    errors.extend(
                        self._remove_exact_volumes(
                            identifiers,
                            expected_label=expected_label,
                            runner=runner,
                        )
                    )
                else:
                    errors.extend(
                        self._remove_exact_networks(
                            identifiers,
                            expected_label=expected_label,
                            runner=runner,
                        )
                    )
            except BaseException as error:
                errors.append(error)
        try:
            if self._exact_label_ids(
                runner=runner,
                resource=resource,
                label=label,
            ):
                errors.append(RuntimeError(f"{description} remain after exhaustive cleanup"))
        except BaseException as error:
            errors.append(error)
        return errors

    def down_and_cleanup(
        self,
        *,
        runner: CommandRunner = run_command,
        upgrade: bool = False,
    ) -> None:
        """Collect bounded diagnostics, exhaust exact artifacts, then re-raise leaks."""
        try:
            self.assert_socket_unchanged()
        except BaseException as authority_error:
            raise BaseExceptionGroup(
                "Phase 10 cleanup authority lost; Docker survivors are unknown",
                [authority_error],
            ) from authority_error

        fail_closed_runner = _FailClosedCommandRunner(runner)
        runner = fail_closed_runner
        errors: list[BaseException] = []
        sandbox_artifacts: tuple[SandboxArtifact, ...] = ()
        direct_artifact_filters: tuple[tuple[Literal["container", "volume"], str], ...] = ()
        try:
            profile = ("--profile", "phase10-upgrade") if upgrade else ()
            try:
                sandbox_artifacts = self.snapshot_sandbox_artifacts(runner=runner)
            except BaseException as error:
                errors.append(error)
            try:
                direct_artifact_filters = self.direct_sandbox_artifact_filters()
            except BaseException as error:
                errors.append(error)
            for diagnostic in (
                self.compose_command(*profile, "ps", "--all", "--format", "json", upgrade=upgrade),
                self.compose_command(
                    *profile, "logs", "--no-color", "--tail", "100", upgrade=upgrade
                ),
            ):
                try:
                    self._run(
                        diagnostic,
                        runner=runner,
                        timeout=60.0,
                        check=False,
                    )
                except BaseException as error:
                    errors.append(error)
            try:
                down = self._run(
                    self.compose_command(
                        *profile,
                        "down",
                        "-v",
                        "--remove-orphans",
                        "--rmi",
                        "local",
                        upgrade=upgrade,
                    ),
                    runner=runner,
                    timeout=300.0,
                    check=False,
                )
                if down.returncode != 0:
                    errors.append(RuntimeError("exact Compose down failed"))
            except BaseException as error:
                errors.append(error)

            invocation_label = f"jhin.phase10.invocation={self.token}"
            errors.extend(
                self._exhaust_exact_labeled_resources(
                    resource="container",
                    label=invocation_label,
                    description="auxiliary containers",
                    runner=runner,
                )
            )

            artifact_filters = tuple(
                dict.fromkeys(
                    (*self.sandbox_artifact_filters(sandbox_artifacts), *direct_artifact_filters)
                )
            )
            for resource, label in artifact_filters:
                errors.extend(
                    self._exhaust_exact_labeled_resources(
                        resource=resource,
                        label=label,
                        description=f"exact sandbox {resource} resources",
                        runner=runner,
                    )
                )

            project_label = f"com.docker.compose.project={self.project}"
            for project_resource in ("container", "volume", "network"):
                errors.extend(
                    self._exhaust_exact_labeled_resources(
                        resource=project_resource,
                        label=project_label,
                        description=f"project {project_resource} resources",
                        runner=runner,
                    )
                )

            network_command = self.docker_command("network", "inspect", self.sandbox_network)
            for attempt in ("initial", "recovery"):
                try:
                    network = self._run(
                        network_command,
                        runner=runner,
                        timeout=30.0,
                        check=False,
                    )
                    if network.returncode == 0:
                        errors.append(
                            RuntimeError(
                                f"cleanup invariant: sandbox network survived {attempt} probe"
                            )
                        )
                        errors.extend(
                            self._remove_exact_networks(
                                (self.sandbox_network,),
                                expected_label=("jhin.sandbox.network", "1"),
                                runner=runner,
                            )
                        )
                    elif network.returncode != 1:
                        errors.append(
                            RuntimeError("failed to verify exact sandbox network cleanup")
                        )
                except BaseException as error:
                    errors.append(error)
            try:
                if (
                    self._run(
                        network_command,
                        runner=runner,
                        timeout=30.0,
                        check=False,
                    ).returncode
                    != 1
                ):
                    errors.append(RuntimeError("exact sandbox network remains"))
            except BaseException as error:
                errors.append(error)

            phase9_tag = self.environment.get("PHASE10_UPGRADE_PHASE9_TAG")
            image_tags = [
                self.runner_image,
                self.sandbox_image,
                *self.compose_auto_image_tags(upgrade=upgrade),
            ]
            if phase9_tag is not None:
                if (
                    re.fullmatch(
                        r"jhin-phase9-agent-worker:[0-9a-f]{12}-[0-9a-f]{8,16}", phase9_tag
                    )
                    is None
                ):
                    errors.append(RuntimeError("frozen Phase 9 cleanup tag is malformed"))
                else:
                    image_tags.append(phase9_tag)
            for image in image_tags:
                for _attempt in ("initial", "recovery"):
                    image_was_present = False
                    try:
                        present = self._run(
                            self.docker_command("image", "inspect", image),
                            runner=runner,
                            timeout=30.0,
                            check=False,
                        )
                        if present.returncode == 0:
                            image_was_present = True
                        elif present.returncode != 1:
                            errors.append(
                                RuntimeError(f"failed to inspect exact image tag {image}")
                            )
                    except BaseException as error:
                        errors.append(error)
                    if image_was_present:
                        try:
                            removed_image = self._run(
                                self.docker_command("image", "rm", image),
                                runner=runner,
                                timeout=120.0,
                                check=False,
                            )
                            if removed_image.returncode != 0:
                                errors.append(
                                    RuntimeError(f"failed to remove exact image tag {image}")
                                )
                        except BaseException as error:
                            errors.append(error)
                try:
                    remaining_image = self._run(
                        self.docker_command("image", "inspect", image),
                        runner=runner,
                        timeout=30.0,
                        check=False,
                    )
                    if remaining_image.returncode != 1:
                        errors.append(
                            RuntimeError(f"exact image tag remains after cleanup: {image}")
                        )
                except BaseException as error:
                    errors.append(error)
            try:
                self.assert_no_preexisting_shared_resources(runner=runner)
            except BaseException as error:
                errors.append(error)
        except BaseException as error:
            errors.append(error)
        finally:
            if fail_closed_runner.survivor is None:
                try:
                    self.assert_socket_unchanged()
                except BaseException as error:
                    errors.append(error)
                if not errors:
                    try:
                        self.remove_recovery_paths()
                    except BaseException as error:
                        errors.append(error)
            elif not any(_contains_owned_process_group_survivor(error) for error in errors):
                errors.append(fail_closed_runner.survivor)
        if errors:
            raise BaseExceptionGroup("Phase 10 cleanup invariants failed", errors)


@dataclass(frozen=True)
class UpgradeScenarioAuthority:
    name: str
    namespace: str
    barrier: BarrierRoot


@dataclass(frozen=True)
class UpgradeHarness:
    """Four-namespace old→current worker swap on one backing authority."""

    authority: ComposeAuthority
    frozen: FrozenPhase9Image
    scenarios: dict[str, UpgradeScenarioAuthority]

    @classmethod
    def from_authority(cls, authority: ComposeAuthority) -> UpgradeHarness:
        environment = authority.environment
        try:
            frozen = FrozenPhase9Image(
                source_ref=environment["PHASE10_UPGRADE_SOURCE_REF"],
                tag=environment["PHASE10_UPGRADE_PHASE9_TAG"],
                image_id=environment["PHASE9_AGENT_IMAGE"],
            )
        except KeyError as error:
            raise ValueError("authority has no prepared upgrade runtime") from error
        if (
            _HEX_REF.fullmatch(frozen.source_ref) is None
            or frozen.tag != authority.phase9_image_tag(frozen.source_ref)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", frozen.image_id) is None
        ):
            raise ValueError("prepared frozen image identity is invalid")
        scenarios: dict[str, UpgradeScenarioAuthority] = {}
        for name in _UPGRADE_SCENARIOS:
            upper = name.upper()
            namespace = environment.get(f"PHASE10_UPGRADE_NAMESPACE_{upper}", "")
            root_value = environment.get(f"PHASE10_UPGRADE_BARRIER_{upper}_HOST", "")
            failpoint = _UPGRADE_FAILPOINTS[name]
            root = Path(root_value)
            if (
                re.fullmatch(r"jhin-p10-[0-9a-f]{8,16}-[a-z]+-[0-9a-f]{8}", namespace) is None
                or root.parent != Path("/tmp")
                or not root.is_dir()
                or root.is_symlink()
                or not (root / failpoint).is_dir()
            ):
                raise ValueError(f"prepared {name} upgrade authority is invalid")
            scenarios[name] = UpgradeScenarioAuthority(
                name=name,
                namespace=namespace,
                barrier=BarrierRoot(root=root, failpoint=failpoint),
            )
        if len({scenario.namespace for scenario in scenarios.values()}) != 4:
            raise ValueError("upgrade namespaces are not distinct")
        return cls(authority=authority, frozen=frozen, scenarios=scenarios)

    def worker_up_command(self, service: str, *, build: bool) -> tuple[str, ...]:
        if (
            re.fullmatch(
                r"phase(?:9-agent|10-(?:agent|tool))-worker-(?:normal|approval|sync|cleanup)",
                service,
            )
            is None
        ):
            raise ValueError("upgrade worker service is outside the exact inventory")
        if service.startswith("phase9-") and build:
            raise ValueError("the frozen Phase 9 service must never build")
        arguments = [
            "--profile",
            "phase10-upgrade",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
        ]
        if build:
            arguments.append("--build")
        arguments.extend(("--wait", "--wait-timeout", "300", service))
        return self.authority.compose_command(*arguments, upgrade=True)

    def phase10_worker_up_commands(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return two nonoverlapping mutations: every tool worker, then every agent."""
        commands: list[tuple[str, ...]] = []
        for kind in ("tool", "agent"):
            services = tuple(f"phase10-{kind}-worker-{scenario}" for scenario in _UPGRADE_SCENARIOS)
            commands.append(
                self.authority.compose_command(
                    "--profile",
                    "phase10-upgrade",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--build",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    *services,
                    upgrade=True,
                )
            )
        return commands[0], commands[1]

    def register_namespaces(self, *, runner: CommandRunner = run_command) -> None:
        for scenario in self.scenarios.values():
            result = self.authority._run(
                self.authority.compose_command(
                    "exec",
                    "-T",
                    "temporal",
                    "tctl",
                    "--address",
                    "temporal:7233",
                    "--ns",
                    scenario.namespace,
                    "namespace",
                    "register",
                    "--retention",
                    "1",
                ),
                runner=runner,
                timeout=60.0,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"failed to register {scenario.name} Temporal namespace")

    def bind_upgrade_task(
        self,
        *,
        task_id: str,
        agent_id: str,
        workflow_id: str,
        runner: CommandRunner = run_command,
    ) -> None:
        if (
            _UUID.fullmatch(task_id) is None
            or _UUID.fullmatch(agent_id) is None
            or re.fullmatch(r"phase10-upgrade-[a-z0-9-]{8,100}", workflow_id) is None
        ):
            raise ValueError("upgrade task binding identity is malformed")
        statement = (
            "UPDATE task SET assigned_agent_id = "
            f"'{agent_id}', temporal_workflow_id = '{workflow_id}', state = 'queued' "
            f"WHERE id = '{task_id}' RETURNING id::text"
        )
        result = self.authority._run(
            self.authority.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-c",
                statement,
            ),
            runner=runner,
            timeout=30.0,
        )
        rows = [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]
        if rows != [task_id]:
            raise RuntimeError("upgrade task binding did not update exactly one task")

    def insert_trigger_invocation(
        self,
        *,
        invocation_id: str,
        workspace_id: str,
        trigger_id: str,
        event_id: str,
        workflow_id: str,
        runner: CommandRunner = run_command,
    ) -> None:
        if (
            any(
                _UUID.fullmatch(value) is None
                for value in (invocation_id, workspace_id, trigger_id, event_id)
            )
            or re.fullmatch(r"phase10-upgrade-sync-[a-z0-9-]{8,100}", workflow_id) is None
        ):
            raise ValueError("upgrade trigger invocation identity is malformed")
        statement = (
            "INSERT INTO trigger_invocation "
            "(id, workspace_id, trigger_id, idempotency_key, event_id, workflow_id, "
            "status, created_at) VALUES ("
            f"'{invocation_id}', '{workspace_id}', '{trigger_id}', "
            f"'phase10-upgrade-{event_id}', '{event_id}', '{workflow_id}', "
            "'started', now()) RETURNING id::text"
        )
        result = self.authority._run(
            self.authority.compose_command(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "jhin",
                "-d",
                "jhin",
                "-At",
                "-c",
                statement,
            ),
            runner=runner,
            timeout=30.0,
        )
        rows = [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]
        if rows != [invocation_id]:
            raise RuntimeError("upgrade trigger invocation was not inserted exactly once")

    def phase9_snapshot_command(
        self,
        scenario: str,
        *,
        include_trigger_prepare: bool,
    ) -> tuple[str, ...]:
        if scenario not in self.scenarios:
            raise ValueError("unknown upgrade snapshot scenario")
        return self.authority.compose_command(
            "--profile",
            "phase10-upgrade",
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "JHIN_TEST_CRASH_BARRIER_DIR=",
            "-e",
            "JHIN_TEST_CRASH_BARRIER_NAME=",
            "-e",
            "JHIN_TEST_CRASH_BARRIER_MATCH=",
            f"phase9-agent-worker-{scenario}",
            "python",
            "-c",
            _PHASE9_SNAPSHOT_WORKER_SCRIPT,
            "trigger" if include_trigger_prepare else "task",
            upgrade=True,
        )

    def run_phase9_snapshot_once(
        self,
        scenario: str,
        *,
        include_trigger_prepare: bool = False,
        runner: CommandRunner = run_command,
    ) -> None:
        self.authority._run(
            self.phase9_snapshot_command(
                scenario,
                include_trigger_prepare=include_trigger_prepare,
            ),
            runner=runner,
            timeout=300.0,
            environment=self._worker_environment(scenario, identity=None),
        )

    def _worker_environment(
        self,
        scenario: str,
        *,
        identity: str | None,
    ) -> dict[str, str]:
        selected = self.scenarios[scenario]
        if identity is not None and _UUID.fullmatch(identity) is None:
            raise ValueError("upgrade barrier identity is malformed")
        environment = self.authority.environment
        upper = scenario.upper()
        environment[f"PHASE10_UPGRADE_BARRIER_{upper}_NAME"] = (
            "" if identity is None else selected.barrier.failpoint
        )
        environment[f"PHASE10_UPGRADE_BARRIER_{upper}_MATCH"] = identity or ""
        return environment

    def assert_stage_topology(
        self,
        stage: UpgradeStage,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, dict[str, Any]]:
        """Require one of the three exact healthy upgrade handoff inventories."""
        expected = self.authority.expected_services
        if stage == "parked-phase9":
            expected.update(f"phase9-agent-worker-{scenario}" for scenario in _UPGRADE_SCENARIOS)
        elif stage == "current-phase10":
            expected.update(_UPGRADE_COMPOSE_AUTO_IMAGE_SERVICES)
        elif stage != "base-only":
            raise ValueError("unknown Phase 10 upgrade stage")
        result = self.authority._run(
            self.authority.compose_command(
                "--profile",
                "phase10-upgrade",
                "ps",
                "--all",
                "--format",
                "json",
                upgrade=True,
            ),
            runner=runner,
            timeout=60.0,
        )
        return parse_compose_ps(_text(result.stdout), expected)

    def start_phase9_worker(
        self,
        scenario: str,
        *,
        identity: str | None = None,
        runner: CommandRunner = run_command,
    ) -> dict[str, Any]:
        service = f"phase9-agent-worker-{scenario}"
        environment = self._worker_environment(scenario, identity=identity)
        self.authority._run(
            self.worker_up_command(service, build=False),
            runner=runner,
            timeout=600.0,
            environment=environment,
        )
        inspected = self.authority.inspect_service(service, runner=runner, upgrade=True)
        if inspected.get("Image") != self.frozen.image_id:
            raise RuntimeError("Phase 9 service did not use the verified frozen image")
        values = inspected.get("Config", {}).get("Env", [])
        observed = dict(
            item.split("=", 1) for item in values if isinstance(item, str) and "=" in item
        )
        selected = self.scenarios[scenario]
        if (
            observed.get("APP_ENV") != "test"
            or observed.get("TEMPORAL_NAMESPACE") != selected.namespace
            or observed.get("JHIN_TEST_CRASH_BARRIER_MATCH") != (identity or "")
        ):
            raise RuntimeError("Phase 9 worker authority differs from the selected scenario")
        self.authority.verify_master_key_readability(
            runner=runner,
            services=(service,),
            upgrade=True,
        )
        return inspected

    def stop_phase9_worker(
        self,
        scenario: str,
        *,
        kill: bool,
        runner: CommandRunner = run_command,
    ) -> None:
        service = f"phase9-agent-worker-{scenario}"
        inspected = self.authority.inspect_service(service, runner=runner, upgrade=True)
        identifier = inspected.get("Id")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("Phase 9 worker has no exact container identity")
        if kill:
            self.authority._run(
                self.authority.docker_command("update", "--restart=no", identifier),
                runner=runner,
                timeout=30.0,
            )
            self.authority._run(
                self.authority.docker_command("kill", "--signal", "SIGKILL", identifier),
                runner=runner,
                timeout=30.0,
            )
        else:
            self.authority._run(
                self.authority.compose_command(
                    "--profile",
                    "phase10-upgrade",
                    "stop",
                    "--timeout",
                    "30",
                    service,
                    upgrade=True,
                ),
                runner=runner,
                timeout=60.0,
            )
        stopped = self.authority._run(
            self.authority.docker_command("inspect", identifier),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError("Phase 9 worker could not be inspected after stop")
        try:
            stopped_payload = json.loads(_text(stopped.stdout))
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Phase 9 worker returned malformed post-stop inspect data"
            ) from error
        if (
            not isinstance(stopped_payload, list)
            or len(stopped_payload) != 1
            or not isinstance(stopped_payload[0], dict)
            or stopped_payload[0].get("Id") != identifier
        ):
            raise RuntimeError("Phase 9 worker identity changed after stop")
        state = stopped_payload[0].get("State")
        if not isinstance(state, dict) or state.get("Running") is not False:
            action = "SIGKILL" if kill else "stop"
            raise RuntimeError(f"Phase 9 worker is still running after {action}")
        removed = self.authority._run(
            self.authority.docker_command("rm", identifier),
            runner=runner,
            timeout=60.0,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError("failed to remove exact Phase 9 worker container")
        remaining_identity = self.authority._run(
            self.authority.docker_command("inspect", identifier),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if remaining_identity.returncode == 0:
            raise RuntimeError("Phase 9 worker survived exact removal: container identity")
        if remaining_identity.returncode != 1:
            raise RuntimeError("Phase 9 worker identity absence is indeterminate")
        remaining_service = self.authority._run(
            self.authority.compose_command(
                "--profile",
                "phase10-upgrade",
                "ps",
                "--all",
                "-q",
                service,
                upgrade=True,
            ),
            runner=runner,
            timeout=30.0,
            check=False,
        )
        if remaining_service.returncode != 0:
            raise RuntimeError("Phase 9 worker service absence is indeterminate")
        if [line for line in _text(remaining_service.stdout).splitlines() if line.strip()]:
            raise RuntimeError("Phase 9 worker survived exact removal: service identity")

    def start_phase10_workers(
        self,
        *,
        runner: CommandRunner = run_command,
    ) -> dict[str, dict[str, Any]]:
        workers: dict[str, dict[str, Any]] = {}
        for kind, command in zip(("tool", "agent"), self.phase10_worker_up_commands(), strict=True):
            self.authority._run(command, runner=runner, timeout=1200.0)
            image_ids: set[str] = set()
            for scenario in _UPGRADE_SCENARIOS:
                service = f"phase10-{kind}-worker-{scenario}"
                inspected = self.authority.inspect_service(service, runner=runner, upgrade=True)
                image_id = inspected.get("Image")
                if (
                    not isinstance(image_id, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
                    or image_id == self.frozen.image_id
                ):
                    raise RuntimeError("Phase 10 worker reused the frozen image")
                image_ids.add(image_id)
                values = inspected.get("Config", {}).get("Env", [])
                observed = dict(
                    item.split("=", 1) for item in values if isinstance(item, str) and "=" in item
                )
                if (
                    observed.get("APP_ENV") != "test"
                    or observed.get("TEMPORAL_NAMESPACE") != self.scenarios[scenario].namespace
                ):
                    raise RuntimeError("Phase 10 upgrade worker namespace is incorrect")
                workers[service] = inspected
            if len(image_ids) != 1:
                raise RuntimeError(f"Phase 10 {kind} workers do not share one current image")
        return workers

    def release(self, scenario: str, identity: str) -> None:
        self.scenarios[scenario].barrier.release(identity)


_LEASE_KEYS = {
    "version",
    "repo",
    "mode",
    "socket_path",
    "socket_gid",
    "token",
    "project",
    "sandbox_network",
    "runner_image",
    "sandbox_image",
    "runtime_dir",
    "master_key_path",
    "barrier_root",
    "docker_executable",
    "environment",
    "published_ports",
    "socket_snapshot",
}


def _authority_record(authority: ComposeAuthority) -> dict[str, Any]:
    return {
        "version": 1,
        "repo": str(authority.repo),
        "mode": authority.mode,
        "socket_path": str(authority.socket_path),
        "socket_gid": authority.socket_gid,
        "token": authority.token,
        "project": authority.project,
        "sandbox_network": authority.sandbox_network,
        "runner_image": authority.runner_image,
        "sandbox_image": authority.sandbox_image,
        "runtime_dir": str(authority.runtime_dir),
        "master_key_path": str(authority.master_key_path),
        "barrier_root": str(authority.barrier_root),
        "docker_executable": authority.docker_executable,
        "environment": authority.environment,
        "published_ports": authority.published_ports,
        "socket_snapshot": (
            None
            if authority.socket_snapshot is None
            else {
                "path": str(authority.socket_snapshot.path),
                "inode": authority.socket_snapshot.inode,
                "mode": authority.socket_snapshot.mode,
                "uid": authority.socket_snapshot.uid,
                "gid": authority.socket_snapshot.gid,
            }
        ),
    }


@dataclass(frozen=True)
class AuthorityLeaseOwnership:
    """Stable proof that this lifecycle published one exact recovery lease."""

    path: Path
    device: int
    inode: int
    authority_token: str


@dataclass
class _AuthorityLeaseTransition:
    """Caller-bound old/new ownership admitted before a filesystem handoff."""

    current: AuthorityLeaseOwnership | None = None
    staged: AuthorityLeaseOwnership | None = None

    def stage(
        self,
        ownership: AuthorityLeaseOwnership,
        *,
        replacing: AuthorityLeaseOwnership | None,
    ) -> None:
        if self.staged is not None or self.current != replacing:
            raise RuntimeError("authority lease ownership transition is inconsistent")
        self.staged = ownership

    def commit(self, ownership: AuthorityLeaseOwnership) -> None:
        if self.staged != ownership:
            raise RuntimeError("authority lease ownership commit is inconsistent")
        self.current = ownership
        self.staged = None

    def candidates(self) -> tuple[AuthorityLeaseOwnership, ...]:
        return tuple(
            candidate for candidate in (self.staged, self.current) if candidate is not None
        )


class _AuthorityLeaseRefreshError(OSError):
    """Refresh failed after the new lease inode became authoritative."""

    def __init__(
        self,
        error: BaseException,
        *,
        ownership: AuthorityLeaseOwnership,
    ) -> None:
        super().__init__(f"authority lease refresh failed after ownership transfer: {error}")
        self.refresh_error = error
        self.ownership = ownership


class _LeaseOwnershipAssignment:
    """Keep catchable signals blocked until the caller binds returned ownership."""

    def __init__(self) -> None:
        self._previous_mask: set[int | signal.Signals] | None = None

    def __enter__(self) -> _LeaseOwnershipAssignment:
        self._previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            _CATCHABLE_SIGNAL_SET,
        )
        return self

    def __exit__(self, *exc_info: Any) -> None:
        del exc_info
        assert self._previous_mask is not None
        signal.pthread_sigmask(signal.SIG_SETMASK, self._previous_mask)


def _validate_direct_tmp_path(path: Path, *, description: str) -> None:
    if path.parent != Path("/tmp") or not path.is_absolute():
        raise ValueError(f"{description} must live directly below /tmp")


def _fsync_tmp_directory() -> None:
    resolved_tmp = Path("/tmp").resolve(strict=True)
    directory = os.open(resolved_tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_all(descriptor: int, payload: bytes, *, description: str) -> None:
    written = 0
    while written < len(payload):
        progress = os.write(descriptor, payload[written:])
        if progress <= 0:
            raise RuntimeError(f"{description} write made zero progress")
        written += progress


def _unlink_exact_file(path: Path, *, device: int, inode: int) -> bool:
    """Unlink one owner-created name only while its stable inode still matches."""
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    if current.st_dev != device or current.st_ino != inode or not stat.S_ISREG(current.st_mode):
        raise RuntimeError(f"refusing to unlink replaced owner-created file: {path}")
    path.unlink()
    return True


def _owned_lease_descriptor(ownership: AuthorityLeaseOwnership) -> int:
    _validate_direct_tmp_path(ownership.path, description="authority lease")
    descriptor = os.open(ownership.path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_dev != ownership.device
            or current.st_ino != ownership.inode
        ):
            raise RuntimeError("authority lease ownership identity changed")
        payload = os.read(descriptor, 1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise RuntimeError("authority lease ownership payload is too large")
        record = json.loads(payload)
        if not isinstance(record, dict) or record.get("token") != ownership.authority_token:
            raise RuntimeError("authority lease ownership token changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _unlink_owned_authority_lease(ownership: AuthorityLeaseOwnership) -> None:
    """Remove only the exact lease inode and token published by this lifecycle."""
    try:
        descriptor = _owned_lease_descriptor(ownership)
    except FileNotFoundError:
        return
    try:
        _unlink_exact_file(
            ownership.path,
            device=ownership.device,
            inode=ownership.inode,
        )
        _fsync_tmp_directory()
    finally:
        os.close(descriptor)


def _unlink_authority_lease_transition(transition: _AuthorityLeaseTransition) -> None:
    """Remove the exact staged or committed inode currently owning the name."""
    candidates = transition.candidates()
    if not candidates:
        return
    path = candidates[0].path
    if any(candidate.path != path for candidate in candidates):
        raise RuntimeError("authority lease transition paths diverged")
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    for candidate in candidates:
        if current.st_dev == candidate.device and current.st_ino == candidate.inode:
            _unlink_owned_authority_lease(candidate)
            return
    if transition.current is None:
        # Initial publication lost an O_EXCL race.  The staged inode still
        # belongs only to the private temp name; preserve the foreign lease.
        return
    raise RuntimeError("authority lease ownership identity changed")


def write_authority_lease(
    authority: ComposeAuthority,
    path: Path,
    *,
    transition: _AuthorityLeaseTransition | None = None,
) -> AuthorityLeaseOwnership:
    """Durably publish a private, no-follow lease without overwriting one."""
    _validate_direct_tmp_path(path, description="authority lease")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError("authority lease path is a symlink")
        raise FileExistsError(f"authority lease already exists: {path}")
    payload = (json.dumps(_authority_record(authority), sort_keys=True) + "\n").encode()
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    temporary_stat = os.fstat(descriptor)
    candidate = AuthorityLeaseOwnership(
        path=path,
        device=temporary_stat.st_dev,
        inode=temporary_stat.st_ino,
        authority_token=authority.token,
    )
    if transition is not None:
        transition.stage(candidate, replacing=None)
    published: AuthorityLeaseOwnership | None = None
    try:
        _write_all(descriptor, payload, description="authority lease")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            try:
                raced = path.lstat()
            except FileNotFoundError:
                raise
            if stat.S_ISLNK(raced.st_mode):
                raise ValueError("authority lease path is a symlink") from error
            raise FileExistsError(f"authority lease already exists: {path}") from error
        published = candidate
        if transition is not None:
            transition.commit(candidate)
        _fsync_tmp_directory()
        _unlink_exact_file(
            temporary,
            device=temporary_stat.st_dev,
            inode=temporary_stat.st_ino,
        )
        _fsync_tmp_directory()
        return published
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        if published is not None:
            try:
                _unlink_owned_authority_lease(published)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "authority lease publication and rollback failed",
                [error, *cleanup_errors],
            ) from error
        raise
    finally:
        os.close(descriptor)
        _unlink_exact_file(
            temporary,
            device=temporary_stat.st_dev,
            inode=temporary_stat.st_ino,
        )


def _replace_authority_lease(
    authority: ComposeAuthority,
    ownership: AuthorityLeaseOwnership,
    *,
    transition: _AuthorityLeaseTransition | None = None,
) -> AuthorityLeaseOwnership:
    """Atomically refresh a validated lease without a missing/partial window."""
    path = ownership.path
    current_descriptor = _owned_lease_descriptor(ownership)
    temporary_descriptor = -1
    temporary_path: Path | None = None
    temporary_stat: os.stat_result | None = None
    try:
        temporary_descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_temporary)
        temporary_stat = os.fstat(temporary_descriptor)
        payload = (json.dumps(_authority_record(authority), sort_keys=True) + "\n").encode()
        _write_all(temporary_descriptor, payload, description="authority lease")
        os.fchmod(temporary_descriptor, 0o600)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        verified_descriptor = _owned_lease_descriptor(ownership)
        os.close(verified_descriptor)
        assert temporary_stat is not None
        refreshed = AuthorityLeaseOwnership(
            path=path,
            device=temporary_stat.st_dev,
            inode=temporary_stat.st_ino,
            authority_token=authority.token,
        )
        if transition is not None:
            transition.stage(refreshed, replacing=ownership)
        os.replace(temporary_path, path)
        temporary_path = None
        if transition is not None:
            transition.commit(refreshed)
        try:
            _fsync_tmp_directory()
        except BaseException as error:
            raise _AuthorityLeaseRefreshError(
                error,
                ownership=refreshed,
            ) from error
        return refreshed
    finally:
        os.close(current_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_path is not None and temporary_stat is not None:
            _unlink_exact_file(
                temporary_path,
                device=temporary_stat.st_dev,
                inode=temporary_stat.st_ino,
            )


def _lease_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"authority lease {field} is malformed")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"authority lease {field} must be absolute")
    return path


def read_authority_lease(
    path: Path,
    *,
    expected_repo: Path,
    allow_missing_recovery_paths: bool = False,
) -> ComposeAuthority:
    """Load an exact owner-only lease and revalidate every derived identity."""
    if path.parent != Path("/tmp") or not path.is_absolute():
        raise ValueError("authority lease must live directly below /tmp")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("authority lease path is a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("authority lease is not a regular file")
    if metadata.st_uid != os.getuid():
        raise ValueError("authority lease has a foreign owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("authority lease must have mode 0600")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        raw = os.read(descriptor, 1_000_001)
    finally:
        os.close(descriptor)
    if len(raw) > 1_000_000:
        raise ValueError("authority lease is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("authority lease is malformed JSON") from error
    if not isinstance(payload, dict) or set(payload) != _LEASE_KEYS or payload["version"] != 1:
        raise ValueError("authority lease schema is invalid")

    repo = _lease_path(payload["repo"], field="repo").resolve(strict=True)
    if repo != expected_repo.resolve(strict=True):
        raise ValueError("authority lease belongs to a different repository")
    mode = _mode(payload["mode"] if isinstance(payload["mode"], str) else "")
    token = payload["token"]
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise ValueError("authority lease token is invalid")
    expected_project = f"jhin-p10-{token}"
    expected_network = f"jhin-p10-sandbox-{token}"
    expected_runner = f"jhin-phase10-sandbox-runner:{token}"
    expected_sandbox = f"jhin-phase10-sandbox:{token}"
    if (
        payload["project"] != expected_project
        or payload["project"] == "jhin"
        or payload["sandbox_network"] != expected_network
        or payload["runner_image"] != expected_runner
        or payload["sandbox_image"] != expected_sandbox
    ):
        raise ValueError("authority lease derived identity is invalid")

    runtime_dir = _lease_path(payload["runtime_dir"], field="runtime_dir")
    barrier_root = _lease_path(payload["barrier_root"], field="barrier_root")
    master_key = _lease_path(payload["master_key_path"], field="master_key_path")
    for runtime_path in (runtime_dir, barrier_root):
        try:
            runtime_metadata = runtime_path.lstat()
        except FileNotFoundError:
            if allow_missing_recovery_paths:
                continue
            raise
        if (
            runtime_path.parent != Path("/tmp")
            or stat.S_ISLNK(runtime_metadata.st_mode)
            or not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != os.getuid()
        ):
            raise ValueError("authority lease runtime identity is invalid")
    if master_key != runtime_dir / "jhin_master_key":
        raise ValueError("authority lease master-key identity is invalid")

    socket_path = _lease_path(payload["socket_path"], field="socket_path")
    socket_gid = payload["socket_gid"]
    if mode == "rootful":
        if type(socket_gid) is not int or socket_gid <= 0:
            raise ValueError("authority lease rootful socket GID is invalid")
    elif socket_gid is not None:
        raise ValueError("authority lease rootless socket GID is invalid")
    executable = payload["docker_executable"]
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ValueError("authority lease Docker executable is invalid")
    environment = payload["environment"]
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("authority lease environment is invalid")
    required_environment = {
        "COMPOSE_PROJECT_NAME": expected_project,
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "DOCKER_HOST": f"unix://{socket_path}",
        "BUILDX_BUILDER": "default",
        "PHASE10_SOCKET_MODE": mode,
        "SANDBOX_NETWORK": expected_network,
        "SANDBOX_RUNNER_IMAGE": expected_runner,
        "SANDBOX_DEFAULT_IMAGE": expected_sandbox,
        "MASTER_KEY_FILE_HOST": str(master_key),
        "JHIN_TEST_CRASH_BARRIER_HOST_DIR": str(barrier_root),
    }
    if any(environment.get(key) != value for key, value in required_environment.items()):
        raise ValueError("authority lease environment identity is invalid")
    raw_child_journal = environment.get(_CHILD_BARRIER_JOURNAL_ENV)
    if raw_child_journal is not None:
        expected_child_journal = runtime_dir / _CHILD_BARRIER_JOURNAL_NAME
        if raw_child_journal != str(expected_child_journal):
            raise ValueError("authority lease child-barrier journal identity is invalid")
        try:
            _validate_child_barrier_journal(expected_child_journal)
        except FileNotFoundError as error:
            if not allow_missing_recovery_paths:
                raise ValueError("authority lease child-barrier journal is invalid") from error
        except RuntimeError as error:
            raise ValueError("authority lease child-barrier journal is invalid") from error
    for variable, _service, _container_port in PUBLISHED_ENDPOINTS:
        expected_value = "127.0.0.1:0" if variable in {"WEB_PORT", "API_PORT"} else "0"
        if environment.get(variable) != expected_value:
            raise ValueError("authority lease published-port identity is invalid")
    if "DOCKER_CONTEXT" in environment or "BUILDKIT_HOST" in environment:
        raise ValueError("authority lease contains a competing Docker target")

    published_payload = payload["published_ports"]
    if not isinstance(published_payload, dict) or any(
        not isinstance(key, str) or type(value) is not int
        for key, value in published_payload.items()
    ):
        raise ValueError("authority lease published ports are invalid")
    published_ports = cast(dict[str, int], published_payload)
    if published_ports:
        expected_port_variables = {
            variable for variable, _service, _container_port in PUBLISHED_ENDPOINTS
        }
        if (
            set(published_ports) != expected_port_variables
            or len(set(published_ports.values())) != len(published_ports)
            or any(not 1 <= port <= 65535 for port in published_ports.values())
        ):
            raise ValueError("authority lease published ports are invalid")

    snapshot_payload = payload["socket_snapshot"]
    snapshot: SocketMetadata | None = None
    if snapshot_payload is not None:
        if not isinstance(snapshot_payload, dict) or set(snapshot_payload) != {
            "path",
            "inode",
            "mode",
            "uid",
            "gid",
        }:
            raise ValueError("authority lease socket snapshot is invalid")
        snapshot_path = _lease_path(snapshot_payload["path"], field="socket snapshot path")
        numeric = tuple(snapshot_payload[key] for key in ("inode", "mode", "uid", "gid"))
        if (
            snapshot_path != socket_path
            or any(type(value) is not int or value < 0 for value in numeric)
            or snapshot_payload["inode"] == 0
            or not stat.S_ISSOCK(snapshot_payload["mode"])
        ):
            raise ValueError("authority lease socket snapshot is invalid")
        snapshot = SocketMetadata(
            path=snapshot_path,
            inode=cast(int, snapshot_payload["inode"]),
            mode=cast(int, snapshot_payload["mode"]),
            uid=cast(int, snapshot_payload["uid"]),
            gid=cast(int, snapshot_payload["gid"]),
        )

    return ComposeAuthority(
        repo=repo,
        mode=mode,
        socket_path=socket_path,
        socket_gid=cast(int | None, socket_gid),
        token=token,
        project=expected_project,
        sandbox_network=expected_network,
        runner_image=expected_runner,
        sandbox_image=expected_sandbox,
        runtime_dir=runtime_dir,
        master_key_path=master_key,
        barrier_root=barrier_root,
        docker_executable=executable,
        _environment_items=tuple(sorted(cast(dict[str, str], environment).items())),
        _published_port_items=tuple(sorted(published_ports.items())),
        socket_snapshot=snapshot,
        _allow_missing_recovery_paths=allow_missing_recovery_paths,
    )


def lease_path_for(repo: Path) -> Path:
    """Return the per-worktree persistent lease name without creating it."""
    identity = hashlib.sha256(str(repo.resolve(strict=True)).encode()).hexdigest()[:16]
    return Path("/tmp") / f"jhin-p10-worktree-{identity}.json"


def persistent_operation_lock_path(repo: Path) -> Path:
    """Return the stable, per-worktree persistent lifecycle lock name."""
    return lease_path_for(repo).with_suffix(".lock")


@dataclass
class _PersistentOperationLock:
    path: Path
    descriptor: int
    device: int
    inode: int

    @classmethod
    def acquire(cls, repo: Path) -> _PersistentOperationLock:
        path = persistent_operation_lock_path(repo)
        _validate_direct_tmp_path(path, description="persistent operation lock")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        created = False
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags)
        try:
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                _fsync_tmp_directory()
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("persistent operation lock is not owner-only")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            named = path.lstat()
            if named.st_dev != metadata.st_dev or named.st_ino != metadata.st_ino:
                raise RuntimeError("persistent operation lock identity changed")
            return cls(
                path=path,
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def __enter__(self) -> _PersistentOperationLock:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        del exc_info
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def build_child_environment(
    authority: ComposeAuthority,
    *,
    ports: Mapping[str, int],
    lease_path: Path,
    expected_tests: int,
    scenario: str = "full",
) -> dict[str, str]:
    """Publish host endpoints before pytest imports integration modules."""
    expected_variables = {variable for variable, _service, _port in PUBLISHED_ENDPOINTS}
    if set(ports) != expected_variables or len(set(ports.values())) != len(ports):
        raise ValueError("resolved port inventory is incomplete or colliding")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports.values()):
        raise ValueError("resolved port inventory contains an invalid port")
    if expected_tests <= 0:
        raise ValueError("strict integration selection requires a positive expected count")
    if lease_path.parent != Path("/tmp") or not lease_path.is_absolute():
        raise ValueError("child authority lease must live directly below /tmp")

    host = "127.0.0.1"
    environment = authority.environment
    environment.update(
        {
            "JHIN_API_URL": f"http://{host}:{ports['API_PORT']}",
            "JHIN_WEB_URL": f"http://{host}:{ports['WEB_PORT']}",
            "JHIN_TEMPORAL_ADDRESS": f"{host}:{ports['TEMPORAL_DEV_PORT']}",
            "JHIN_TEMPORAL_UI_URL": f"http://{host}:{ports['TEMPORAL_UI_DEV_PORT']}",
            "JHIN_NATS_URL": f"nats://{host}:{ports['NATS_DEV_PORT']}",
            "JHIN_NATS_MONITOR_URL": f"http://{host}:{ports['NATS_MONITOR_DEV_PORT']}",
            "JHIN_FAKE_PROVIDER_URL": f"http://{host}:{ports['FAKE_PROVIDER_DEV_PORT']}",
            "JHIN_FAKE_GITHUB_URL": f"http://{host}:{ports['FAKE_GITHUB_DEV_PORT']}",
            "JHIN_FAKE_LINEAR_URL": f"http://{host}:{ports['FAKE_LINEAR_DEV_PORT']}",
            "JHIN_FAKE_VERCEL_URL": f"http://{host}:{ports['FAKE_VERCEL_DEV_PORT']}",
            "JHIN_FAKE_SUPABASE_URL": f"http://{host}:{ports['FAKE_SUPABASE_DEV_PORT']}",
            "SANDBOX_RUNNER_DEV_URL": f"http://{host}:{ports['SANDBOX_RUNNER_DEV_PORT']}",
            "JHIN_POSTGRES_HOST": host,
            "JHIN_POSTGRES_PORT": str(ports["POSTGRES_DEV_PORT"]),
            "JHIN_PHASE9_DB_READER_DSN": (
                f"postgresql://jhin_reader:reader-pass@{host}:"
                f"{ports['FAKE_SUPABASE_DB_DEV_PORT']}/supabase_fixture"
            ),
            "JHIN_PHASE9_DB_WRITER_DSN": (
                f"postgresql://jhin_writer:writer-pass@{host}:"
                f"{ports['FAKE_SUPABASE_DB_DEV_PORT']}/supabase_fixture"
            ),
            "JHIN_PHASE9_DB_ADMIN_DSN": (
                f"postgresql://postgres:phase9-fixture-admin-only@{host}:"
                f"{ports['FAKE_SUPABASE_DB_DEV_PORT']}/supabase_fixture"
            ),
            "JHIN_PHASE10_AUTHORITY_LEASE": str(lease_path),
            "JHIN_PHASE10_STRICT_SELECTION": "1",
            "JHIN_PHASE10_EXPECTED_TESTS": str(expected_tests),
            "JHIN_PHASE10_SCENARIO": scenario,
            "JHIN_TEST_COMPOSE_PROJECT": authority.project,
        }
    )
    return environment


def _contains_owned_process_group_survivor(error: BaseException | None) -> bool:
    if isinstance(error, _OwnedProcessGroupSurvived):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_owned_process_group_survivor(nested) for nested in error.exceptions)
    return False


def _only_lifecycle_signal(error: BaseException | None) -> int | None:
    """Return one signum only when every nested failure is that lifecycle signal."""
    if error is None:
        return None
    if isinstance(error, _LifecycleSignal):
        return error.signum
    if not isinstance(error, BaseExceptionGroup) or not error.exceptions:
        return None
    nested = [_only_lifecycle_signal(item) for item in error.exceptions]
    if any(signum is None for signum in nested):
        return None
    signums = {cast(int, signum) for signum in nested}
    if len(signums) != 1:
        return None
    return next(iter(signums))


@dataclass
class _FailClosedCommandRunner:
    """Latch the first unexhausted group and issue no later external command."""

    delegate: CommandRunner
    survivor: BaseException | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if self.survivor is not None:
            raise self.survivor
        try:
            return self.delegate(*args, **kwargs)
        except BaseException as error:
            if _contains_owned_process_group_survivor(error):
                self.survivor = error
            raise


def execute_one_shot(
    authority: ComposeAuthority,
    *,
    scenario: LiveScenario,
    lease_path: Path | None = None,
    runner: CommandRunner = run_owned_command,
    child_runner: CommandRunner = run_owned_command,
    _signal_lifecycle: _CatchableSignalLifecycle | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Own stack→child-pytest→exhaustive-cleanup as one signal-safe lifecycle."""
    lease = (
        lease_path
        if lease_path is not None
        else Path("/tmp") / f"jhin-p10-live-{authority.token}.json"
    )
    if lease.parent != Path("/tmp") or not lease.is_absolute():
        raise ValueError("one-shot authority lease must live directly below /tmp")

    signal_lifecycle = _signal_lifecycle or _CatchableSignalLifecycle.prepare()
    live_authority = authority
    cleaned = False
    cleaning = False
    external_started = False
    lease_ownership: AuthorityLeaseOwnership | None = None
    lease_transition = _AuthorityLeaseTransition()
    cleanup_errors: list[BaseException] = []

    def cleanup() -> None:
        nonlocal cleaned, cleaning
        if cleaned or cleaning:
            return
        cleaning = True
        try:
            try:
                signal_lifecycle.ignore()
            except BaseException as error:
                cleanup_errors.append(error)
                return
            if not external_started:
                local_error: BaseException | None = None
                try:
                    live_authority.remove_runtime_paths()
                except BaseException as error:
                    local_error = error
                    cleanup_errors.append(error)
                if local_error is None:
                    try:
                        _unlink_authority_lease_transition(lease_transition)
                    except BaseException as error:
                        cleanup_errors.append(error)
                        local_error = error
                cleaned = local_error is None
                return

            cleanup_runner = _FailClosedCommandRunner(runner)
            down_error: BaseException | None = None
            try:
                live_authority.down_and_cleanup(
                    runner=cleanup_runner,
                    upgrade=scenario.upgrade,
                )
            except BaseException as error:
                down_error = error
                cleanup_errors.append(error)
            if cleanup_runner.survivor is not None and (
                down_error is None or not _contains_owned_process_group_survivor(down_error)
            ):
                cleanup_errors.append(cleanup_runner.survivor)
            if down_error is None:
                try:
                    _unlink_authority_lease_transition(lease_transition)
                except BaseException as error:
                    cleanup_errors.append(error)
                    down_error = error
            cleaned = down_error is None
        finally:
            cleaning = False

    atexit.register(cleanup)

    primary_error: BaseException | None = None
    result: subprocess.CompletedProcess[Any] | None = None
    failure_output_emitted = False
    try:
        signal_lifecycle.activate()
        live_authority = live_authority.with_child_barrier_journal()
        scenario_name = next(
            (name for name, candidate in LIVE_SCENARIOS.items() if candidate == scenario),
            "custom",
        )
        with _LeaseOwnershipAssignment():
            lease_ownership = write_authority_lease(
                live_authority,
                lease,
                transition=lease_transition,
            )
        external_started = True
        if scenario.start_stack:
            ports = live_authority.start_stack(runner=runner)
            live_authority = live_authority.with_published_ports(ports)
            with _LeaseOwnershipAssignment():
                try:
                    lease_ownership = _replace_authority_lease(
                        live_authority,
                        lease_ownership,
                        transition=lease_transition,
                    )
                except _AuthorityLeaseRefreshError as error:
                    lease_ownership = error.ownership
                    raise error.refresh_error from error
            if scenario.upgrade:
                source_ref = read_phase9_source_ref(
                    live_authority.repo,
                    runner=runner,
                    environment=live_authority.environment,
                )
                planned_environment = live_authority.environment
                planned_environment.update(
                    {
                        "PHASE10_UPGRADE_PHASE9_TAG": live_authority.phase9_image_tag(source_ref),
                        "PHASE10_UPGRADE_SOURCE_REF": source_ref,
                    }
                )
                live_authority = replace(
                    live_authority,
                    _environment_items=tuple(sorted(planned_environment.items())),
                )
                with _LeaseOwnershipAssignment():
                    try:
                        lease_ownership = _replace_authority_lease(
                            live_authority,
                            lease_ownership,
                            transition=lease_transition,
                        )
                    except _AuthorityLeaseRefreshError as error:
                        lease_ownership = error.ownership
                        raise error.refresh_error from error
                frozen = live_authority.build_phase9_agent_image(source_ref, runner=runner)
                try:
                    previous_mask = signal.pthread_sigmask(
                        signal.SIG_BLOCK,
                        _CATCHABLE_SIGNAL_SET,
                    )
                    try:
                        live_authority = live_authority.with_upgrade_runtime(frozen)
                        with _LeaseOwnershipAssignment():
                            try:
                                lease_ownership = _replace_authority_lease(
                                    live_authority,
                                    lease_ownership,
                                    transition=lease_transition,
                                )
                            except _AuthorityLeaseRefreshError as error:
                                lease_ownership = error.ownership
                                raise error.refresh_error from error
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                except BaseException as setup_error:
                    if _contains_owned_process_group_survivor(setup_error):
                        raise
                    try:
                        live_authority.remove_phase9_agent_image(
                            frozen.source_ref,
                            runner=runner,
                        )
                    except BaseException as cleanup_error:
                        raise BaseExceptionGroup(
                            "upgrade runtime setup failed and frozen image survivor is unknown",
                            [setup_error, cleanup_error],
                        ) from setup_error
                    raise
            child_environment = build_child_environment(
                live_authority,
                ports=ports,
                lease_path=lease,
                expected_tests=scenario.expected_tests,
                scenario=scenario_name,
            )
        else:
            live_authority.preflight(runner=runner)
            child_environment = live_authority.environment
            child_environment.update(
                {
                    "JHIN_PHASE10_AUTHORITY_LEASE": str(lease),
                    "JHIN_PHASE10_STRICT_SELECTION": "1",
                    "JHIN_PHASE10_EXPECTED_TESTS": str(scenario.expected_tests),
                    "JHIN_PHASE10_SCENARIO": scenario_name,
                    "JHIN_TEST_COMPOSE_PROJECT": live_authority.project,
                }
            )
        try:
            result = child_runner(
                build_live_pytest_command(scenario),
                env=child_environment,
                cwd=live_authority.repo,
                timeout=5400.0,
                check=True,
                input_bytes=None,
            )
        except subprocess.CalledProcessError as error:
            emit_live_child_failure_output(error, environment=child_environment)
            failure_output_emitted = True
            raise
        if result.returncode != 0:
            child_error = subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
            emit_live_child_failure_output(child_error, environment=child_environment)
            failure_output_emitted = True
            raise child_error
    except BaseException as error:
        if isinstance(error, subprocess.CalledProcessError) and not failure_output_emitted:
            emit_live_failure_output(
                error,
                environment=live_authority.environment,
                context="setup",
            )
        primary_error = error
    finally:
        child_group_survived = _contains_owned_process_group_survivor(primary_error)
        transition_safe = True
        try:
            signal_lifecycle.ignore()
        except BaseException as error:
            transition_safe = False
            cleanup_errors.append(error)
        if not child_group_survived and transition_safe:
            cleanup()
        # If bounded SIGKILL could not prove the child group absent, even
        # local lease teardown is withheld: descendants may still be using
        # that authority and no cleanup may race them.
        atexit.unregister(cleanup)
        try:
            signal_lifecycle.restore()
        except BaseException as error:
            cleanup_errors.append(error)

    if isinstance(primary_error, _LifecycleSignal):
        primary_error = SystemExit(128 + primary_error.signum)

    if primary_error is not None and cleanup_errors:
        raise BaseExceptionGroup(
            "Phase 10 lifecycle and cleanup both failed",
            [primary_error, *cleanup_errors],
        )
    if primary_error is not None:
        raise primary_error
    if cleanup_errors:
        raise BaseExceptionGroup("Phase 10 lifecycle cleanup failed", cleanup_errors)
    assert result is not None
    return result


def _execute_selected_one_shot(
    *,
    repo: Path,
    mode: str,
    scenario: LiveScenario,
) -> subprocess.CompletedProcess[Any]:
    """Acquire signal ownership before allocating the one-shot authority."""
    signal_lifecycle = _CatchableSignalLifecycle.prepare()
    authority: ComposeAuthority | None = None
    try:
        authority = select_live_authority(repo=repo, mode=mode)
        return execute_one_shot(
            authority,
            scenario=scenario,
            _signal_lifecycle=signal_lifecycle,
        )
    except BaseException:
        if authority is not None and signal_lifecycle.state == "prepared":
            authority.remove_runtime_paths()
        if signal_lifecycle.state != "closed":
            signal_lifecycle.restore()
        raise


def _persistent_up(
    *,
    repo: Path,
    mode: str,
    runner: CommandRunner = run_owned_command,
) -> ComposeAuthority:
    with _PersistentOperationLock.acquire(repo):
        return _persistent_up_locked(repo=repo, mode=mode, runner=runner)


def _persistent_up_locked(
    *,
    repo: Path,
    mode: str,
    runner: CommandRunner,
) -> ComposeAuthority:
    lease = lease_path_for(repo)
    if lease.exists() or lease.is_symlink():
        raise FileExistsError(f"persistent Phase 10 lease already exists: {lease}")
    signal_lifecycle = _CatchableSignalLifecycle.prepare()
    authority: ComposeAuthority | None = None
    external_started = False
    lease_ownership: AuthorityLeaseOwnership | None = None
    lease_transition = _AuthorityLeaseTransition()
    cleaning = False
    cleaned = False
    cleanup_errors: list[BaseException] = []

    def cleanup() -> None:
        nonlocal cleaning, cleaned
        if authority is None or cleaning or cleaned:
            return
        cleaning = True
        try:
            try:
                signal_lifecycle.ignore()
            except BaseException as error:
                cleanup_errors.append(error)
                return
            if not external_started:
                local_error: BaseException | None = None
                try:
                    authority.remove_runtime_paths()
                except BaseException as error:
                    local_error = error
                    cleanup_errors.append(error)
                if local_error is None:
                    try:
                        _unlink_authority_lease_transition(lease_transition)
                    except BaseException as error:
                        cleanup_errors.append(error)
                        local_error = error
                cleaned = local_error is None
                return

            cleanup_runner = _FailClosedCommandRunner(runner)
            down_error: BaseException | None = None
            try:
                authority.down_and_cleanup(runner=cleanup_runner)
            except BaseException as error:
                down_error = error
                cleanup_errors.append(error)
            if cleanup_runner.survivor is not None and (
                down_error is None or not _contains_owned_process_group_survivor(down_error)
            ):
                cleanup_errors.append(cleanup_runner.survivor)
            if down_error is None:
                try:
                    _unlink_authority_lease_transition(lease_transition)
                except BaseException as error:
                    cleanup_errors.append(error)
                    down_error = error
            cleaned = down_error is None
        finally:
            cleaning = False

    primary_error: BaseException | None = None
    atexit.register(cleanup)
    try:
        authority = select_live_authority(repo=repo, mode=mode)
        signal_lifecycle.activate()
        with _LeaseOwnershipAssignment():
            lease_ownership = write_authority_lease(
                authority,
                lease,
                transition=lease_transition,
            )
        external_started = True
        ports = authority.start_stack(runner=runner)
        authority = authority.with_published_ports(ports)
        with _LeaseOwnershipAssignment():
            try:
                lease_ownership = _replace_authority_lease(
                    authority,
                    lease_ownership,
                    transition=lease_transition,
                )
            except _AuthorityLeaseRefreshError as error:
                lease_ownership = error.ownership
                raise error.refresh_error from error
    except BaseException as error:
        primary_error = error
    finally:
        transition_safe = True
        try:
            signal_lifecycle.ignore()
        except BaseException as error:
            transition_safe = False
            cleanup_errors.append(error)
        if (
            primary_error is not None
            and not _contains_owned_process_group_survivor(primary_error)
            and transition_safe
        ):
            cleanup()
        atexit.unregister(cleanup)
        try:
            signal_lifecycle.restore()
        except BaseException as error:
            cleanup_errors.append(error)

    if isinstance(primary_error, _LifecycleSignal):
        primary_error = SystemExit(128 + primary_error.signum)
    if primary_error is not None and cleanup_errors:
        raise BaseExceptionGroup(
            "persistent Phase 10 start and cleanup both failed",
            [primary_error, *cleanup_errors],
        )
    if primary_error is not None:
        raise primary_error
    if cleanup_errors:
        raise BaseExceptionGroup("persistent Phase 10 startup cleanup failed", cleanup_errors)
    assert authority is not None
    return authority


def _load_persistent(
    repo: Path,
    *,
    allow_missing_recovery_paths: bool = False,
) -> tuple[Path, ComposeAuthority, AuthorityLeaseOwnership]:
    lease = lease_path_for(repo)
    authority = read_authority_lease(
        lease,
        expected_repo=repo,
        allow_missing_recovery_paths=allow_missing_recovery_paths,
    )
    metadata = lease.lstat()
    ownership = AuthorityLeaseOwnership(
        path=lease,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        authority_token=authority.token,
    )
    descriptor = _owned_lease_descriptor(ownership)
    os.close(descriptor)
    return lease, authority, ownership


def _persistent_down(
    *,
    repo: Path,
    runner: CommandRunner = run_owned_command,
) -> None:
    with _PersistentOperationLock.acquire(repo):
        _persistent_down_locked(repo=repo, runner=runner)


def _persistent_down_locked(
    *,
    repo: Path,
    runner: CommandRunner,
) -> None:
    signal_lifecycle = _CatchableSignalLifecycle.prepare()
    lease: Path | None = None
    lease_ownership: AuthorityLeaseOwnership | None = None
    authority: ComposeAuthority | None = None
    primary_error: BaseException | None = None
    conclusive_signal: int | None = None
    transition_errors: list[BaseException] = []
    try:
        lease, authority, lease_ownership = _load_persistent(
            repo,
            allow_missing_recovery_paths=True,
        )
        signal_lifecycle.activate()
        authority.assert_socket_unchanged()
        authority.down_and_cleanup(runner=runner)
    except BaseException as error:
        primary_error = error
        nested_signal = _only_lifecycle_signal(error)
        if (
            nested_signal is not None
            and authority is not None
            and not authority.runtime_dir.exists()
            and not authority.barrier_root.exists()
        ):
            conclusive_signal = nested_signal
    finally:
        transition_safe = True
        try:
            signal_lifecycle.ignore()
        except BaseException as error:
            transition_safe = False
            transition_errors.append(error)
        if (
            (primary_error is None or conclusive_signal is not None)
            and transition_safe
            and lease is not None
            and lease_ownership is not None
        ):
            try:
                _unlink_owned_authority_lease(lease_ownership)
            except BaseException as error:
                transition_errors.append(error)
        try:
            signal_lifecycle.restore()
        except BaseException as error:
            transition_errors.append(error)

    if conclusive_signal is not None:
        primary_error = SystemExit(128 + conclusive_signal)
    elif isinstance(primary_error, _LifecycleSignal):
        primary_error = SystemExit(128 + primary_error.signum)
    if primary_error is not None and transition_errors:
        raise BaseExceptionGroup(
            "persistent Phase 10 teardown and signal handoff both failed",
            [primary_error, *transition_errors],
        )
    if primary_error is not None:
        raise primary_error
    if transition_errors:
        raise BaseExceptionGroup("persistent Phase 10 teardown handoff failed", transition_errors)


def _persistent_compose(
    *,
    repo: Path,
    arguments: Sequence[str],
    runner: CommandRunner = run_owned_command,
) -> subprocess.CompletedProcess[Any]:
    with _PersistentOperationLock.acquire(repo):
        return _persistent_compose_locked(
            repo=repo,
            arguments=arguments,
            runner=runner,
        )


def _persistent_compose_locked(
    *,
    repo: Path,
    arguments: Sequence[str],
    runner: CommandRunner,
) -> subprocess.CompletedProcess[Any]:
    signal_lifecycle = _CatchableSignalLifecycle.prepare()
    primary_error: BaseException | None = None
    transition_errors: list[BaseException] = []
    result: subprocess.CompletedProcess[Any] | None = None
    try:
        _lease, authority, _lease_ownership = _load_persistent(repo)
        signal_lifecycle.activate()
        authority.assert_socket_unchanged()
        selected_arguments = validate_persistent_compose_arguments(arguments)
        result = authority._run(
            authority.compose_command(*selected_arguments),
            runner=runner,
            timeout=_PERSISTENT_COMPOSE_TIMEOUT_SECONDS,
        )
    except BaseException as error:
        primary_error = error
    finally:
        try:
            signal_lifecycle.ignore()
        except BaseException as error:
            transition_errors.append(error)
        try:
            signal_lifecycle.restore()
        except BaseException as error:
            transition_errors.append(error)

    if isinstance(primary_error, _LifecycleSignal):
        primary_error = SystemExit(128 + primary_error.signum)
    if primary_error is not None and transition_errors:
        raise BaseExceptionGroup(
            "persistent Phase 10 Compose command and signal handoff both failed",
            [primary_error, *transition_errors],
        )
    if primary_error is not None:
        raise primary_error
    if transition_errors:
        raise BaseExceptionGroup("persistent Phase 10 Compose handoff failed", transition_errors)
    assert result is not None
    return result


_PERSISTENT_COMPOSE_ALLOWLIST = frozenset(
    {
        ("--profile", "build", "build", "sandbox-image"),
        ("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
        ("run", "--rm", "--no-deps", "api", "jhin-seed-dev"),
    }
)


def validate_persistent_compose_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    """Accept only the three typed, nonpublishing persistent-stack operations."""
    selected = tuple(arguments)
    if selected not in _PERSISTENT_COMPOSE_ALLOWLIST:
        raise ValueError("persistent Compose command is outside the typed allowlist")
    return selected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="isolated Phase 10 Docker authority")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="start, test, and clean one live scenario")
    run.add_argument("--mode", choices=("rootful", "rootless"), required=True)
    run.add_argument("--scenario", choices=tuple(LIVE_SCENARIOS), required=True)

    up = subcommands.add_parser("up", help="start one persistent isolated stack")
    up.add_argument("--mode", choices=("rootful", "rootless"), required=True)

    subcommands.add_parser("down", help="stop the persistent isolated stack")
    compose = subcommands.add_parser("compose", help="run against the persistent exact vector")
    compose.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    if args.command == "run":
        _execute_selected_one_shot(
            repo=repo,
            mode=cast(str, args.mode),
            scenario=LIVE_SCENARIOS[cast(str, args.scenario)],
        )
        return 0
    if args.command == "up":
        authority = _persistent_up(repo=repo, mode=cast(str, args.mode))
        endpoints = build_child_environment(
            authority,
            ports=authority.published_ports,
            lease_path=lease_path_for(repo),
            expected_tests=1,
            scenario="persistent",
        )
        for key in (
            "JHIN_API_URL",
            "JHIN_WEB_URL",
            "JHIN_TEMPORAL_ADDRESS",
            "JHIN_NATS_URL",
            "SANDBOX_RUNNER_DEV_URL",
        ):
            print(f"{key}={endpoints[key]}")
        return 0
    if args.command == "down":
        _persistent_down(repo=repo)
        return 0
    if args.command == "compose":
        arguments = tuple(cast(list[str], args.arguments))
        if arguments[:1] == ("--",):
            arguments = arguments[1:]
        if not arguments:
            raise ValueError("compose requires an explicit subcommand")
        result = _persistent_compose(repo=repo, arguments=arguments)
        if result.stdout:
            sys.stdout.write(_text(result.stdout))
        if result.stderr:
            sys.stderr.write(_text(result.stderr))
        return result.returncode
    raise AssertionError("argparse returned an unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
