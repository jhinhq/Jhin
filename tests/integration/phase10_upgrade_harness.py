"""Isolated Docker/Compose authority for Phase 10 live acceptance.

This module is deliberately importable without Docker.  Pure contract tests
exercise parsing and command construction; the CLI at the bottom is the only
entry point permitted to mutate a live daemon for Phase 10 acceptance.
"""

from __future__ import annotations

import argparse
import atexit
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
_TOKEN = re.compile(r"[0-9a-f]{8,16}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
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


class ComposePsError(ValueError):
    """The selected project is not the exact healthy service topology."""


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


def _ignore_catchable_signals() -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN)
        except ValueError:
            previous_handlers.clear()
            break
    return previous_handlers


def _restore_signal_handlers(previous_handlers: Mapping[int, Any]) -> None:
    for signum, previous in previous_handlers.items():
        signal.signal(signum, previous)


def _terminate_owned_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group: int,
) -> tuple[Any, Any]:
    """Terminate, reap, and prove absence of one isolated process group."""
    previous_handlers = _ignore_catchable_signals()
    stdout: Any = None
    stderr: Any = None
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
        return stdout, stderr
    finally:
        _restore_signal_handlers(previous_handlers)


def run_live_child_command(
    command: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    check: bool,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run pytest in one owned session and exhaust all of its descendants."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=input_bytes is None,
        start_new_session=True,
    )
    process_group = process.pid
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired as error:
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
        try:
            _terminate_owned_process_group(process, process_group=process_group)
        except BaseException as teardown_error:
            raise BaseExceptionGroup(
                "live child failed and its process group survived",
                [error, teardown_error],
            ) from error
        raise

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
    for service, row in parsed.items():
        state = str(row.get("State", "")).lower()
        if state != "running":
            raise ComposePsError(f"service {service} is not running: {state or 'missing'}")
        health_value = row.get("Health", "")
        health = "" if health_value is None else str(health_value).lower()
        if health != "healthy":
            raise ComposePsError(f"service {service} is unhealthy: {health or 'missing health'}")
    return parsed


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
                if not stat.S_ISREG(metadata.st_mode) or marker.read_bytes() != b"arrived\n":
                    raise RuntimeError("barrier arrival marker is malformed")
                return marker.name.removesuffix(".arrived")
            if time.monotonic() >= deadline:
                raise TimeoutError("worker did not reach the selected crash barrier")
            time.sleep(0.02)

    def cleanup(self) -> None:
        if not self.root.exists():
            return
        self._validate()
        os.chmod(self.root, 0o700, follow_symlinks=False)
        shutil.rmtree(self.root)


def create_barrier_root(failpoint: str) -> BarrierRoot:
    if not failpoint or "/" in failpoint or failpoint in {".", ".."}:
        raise ValueError("invalid barrier failpoint")
    root = Path(tempfile.mkdtemp(prefix="jhin-p10-barrier-", dir="/tmp"))
    selected = root / failpoint
    try:
        selected.mkdir()
        os.chmod(selected, 0o1777, follow_symlinks=False)
        os.chmod(root, 0o711, follow_symlinks=False)
    except BaseException:
        os.chmod(root, 0o700, follow_symlinks=False)
        shutil.rmtree(root)
        raise
    return BarrierRoot(root=root, failpoint=failpoint)


def _events(history_or_events: Any) -> Sequence[Any]:
    events = getattr(history_or_events, "events", history_or_events)
    if not isinstance(events, Sequence):
        events = tuple(events)
    return cast(Sequence[Any], events)


def activity_schedule_pairs(history_or_events: Any) -> list[tuple[str, str]]:
    """Extract scheduled activity name/queue pairs in history order."""
    pairs: list[tuple[str, str]] = []
    for event in _events(history_or_events):
        attributes = getattr(event, "activity_task_scheduled_event_attributes", None)
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
    """Count starts for all schedules of one exact activity name."""
    scheduled_ids: set[int] = set()
    events = _events(history_or_events)
    for event in events:
        attributes = getattr(event, "activity_task_scheduled_event_attributes", None)
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
        attributes = getattr(event, "activity_task_started_event_attributes", None)
        scheduled_event_id = getattr(attributes, "scheduled_event_id", None)
        if scheduled_event_id in scheduled_ids:
            count += 1
    return count


def read_phase9_source_ref(repo: Path) -> str:
    """Load the frozen Phase 9 commit and prove it is an ancestor of HEAD."""
    path = repo / "packages/workflows/tests/fixtures/phase9_temporal/phase9-ref.txt"
    source_ref = path.read_text(encoding="utf-8").strip()
    if _HEX_REF.fullmatch(source_ref) is None:
        raise ValueError("Phase 9 source ref must be exactly forty lowercase hex characters")
    resolved = subprocess.run(
        ["git", "cat-file", "-e", f"{source_ref}^{{commit}}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_ref, "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
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
        archive = subprocess.run(
            self.phase9_archive_command(source_ref),
            cwd=self.repo,
            env=self.environment,
            check=False,
            capture_output=True,
            timeout=120.0,
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

    def worker_recreate_command(self, service: str) -> tuple[str, ...]:
        if service not in {"agent-worker", "tool-worker"}:
            raise ValueError("only a Phase 10 worker may be recreated")
        return self.compose_command(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--build",
            "--wait",
            "--wait-timeout",
            "300",
            service,
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

    def recreate_worker(
        self,
        service: str,
        *,
        barrier: BarrierRoot | None = None,
        identity: str | None = None,
        runner: CommandRunner = run_command,
    ) -> None:
        environment = self.worker_environment(barrier=barrier, identity=identity)
        self._run(
            self.worker_recreate_command(service),
            runner=runner,
            timeout=1200.0,
            environment=environment,
        )
        result = self._run(
            self.compose_command("ps", "--all", "--format", "json"),
            runner=runner,
            timeout=60.0,
            environment=environment,
        )
        parse_compose_ps(_text(result.stdout), self.expected_services)
        inspected = self.inspect_service(service, runner=runner)
        values = inspected.get("Config", {}).get("Env", [])
        if not isinstance(values, list):
            raise RuntimeError("worker inspect omitted its environment")
        observed = dict(
            item.split("=", 1) for item in values if isinstance(item, str) and "=" in item
        )
        expected = {
            key: environment[key]
            for key in (
                "APP_ENV",
                "JHIN_TEST_CRASH_BARRIER_DIR",
                "JHIN_TEST_CRASH_BARRIER_NAME",
                "JHIN_TEST_CRASH_BARRIER_MATCH",
            )
        }
        if any(observed.get(key) != value for key, value in expected.items()):
            raise RuntimeError("worker barrier environment differs from the selected identity")

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
        result = self._run(
            self.compose_command("ps", "--all", "--format", "json", upgrade=upgrade),
            runner=runner,
            timeout=60.0,
        )
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="strict")
        return parse_compose_ps(
            cast(str, output),
            self.expected_services if expected_services is None else expected_services,
        )

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

    def run_noop_sandbox_job(self, *, timeout: float = 30.0) -> dict[str, Any]:
        port = self.published_ports.get("SANDBOX_RUNNER_DEV_PORT")
        if port is None:
            raise RuntimeError("sandbox runner host endpoint was not resolved")
        job_id = secrets.token_hex(12)
        self.record_direct_sandbox_job(job_id)
        endpoint = f"http://127.0.0.1:{port}"
        headers = {
            "Authorization": f"Bearer {self.environment['SANDBOX_RUNNER_TOKEN']}",
            "Content-Type": "application/json",
        }
        body = json.dumps(
            {
                "job_id": job_id,
                "command": ["python3", "-c", "print('phase10-noop')"],
                "network_policy": "none",
            }
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

    @staticmethod
    def sandbox_job_label(job_id: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", job_id) is None:
            raise ValueError("sandbox job identity is malformed")
        return f"jhin.sandbox.job={job_id}"

    @property
    def direct_sandbox_job_ledger(self) -> Path:
        return self.runtime_dir / "direct-sandbox-jobs.json"

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
            os.write(descriptor, encoded)
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
        return tuple(
            ("container", self.sandbox_job_label(job_id)) for job_id in self.direct_sandbox_jobs()
        )

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

    def start_inspectable_sandbox_job(
        self,
        *,
        runner: CommandRunner = run_command,
        timeout: float = 30.0,
    ) -> RunningSandboxJob:
        job_id = f"phase10-security-{secrets.token_hex(8)}"
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
        try:
            while time.monotonic() < deadline:
                identifiers = self._exact_label_ids(
                    runner=runner,
                    resource="container",
                    label=self.sandbox_job_label(job_id),
                )
                if len(identifiers) > 1:
                    raise RuntimeError("sandbox job label resolved multiple containers")
                if identifiers:
                    inspected = self._run(
                        self.docker_command("inspect", identifiers[0]),
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
                        or payload[0].get("Id") != identifiers[0]
                        or payload[0].get("State", {}).get("Running") is not True
                    ):
                        raise RuntimeError("sandbox job inspect identity is not running")
                    return RunningSandboxJob(
                        job_id=job_id,
                        container_id=identifiers[0],
                        container=cast(dict[str, Any], payload[0]),
                    )
                time.sleep(0.05)
            raise TimeoutError("inspectable sandbox job did not start")
        except BaseException:
            self.cancel_sandbox_job(job_id, runner=runner, timeout=30.0)
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

    def _assert_service_ready(
        self,
        service: str,
        *,
        runner: CommandRunner,
    ) -> None:
        result = self._run(
            self.compose_command("ps", "--all", "--format", "json", service),
            runner=runner,
            timeout=60.0,
        )
        parse_compose_ps(_text(result.stdout), {service})

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
            self._assert_service_ready("rootless-docker-transport", runner=runner)
        self._assert_service_ready("sandbox-runner", runner=runner)
        self.assert_ready(runner=runner)
        self._run(
            self.compose_command("run", "--rm", "--no-deps", "api", "jhin-db-migrate"),
            runner=runner,
            timeout=300.0,
        )
        self.assert_ready(runner=runner)
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
            command = self.docker_command("ps", "-aq", "--filter", f"label={label}")
        elif resource == "volume":
            command = self.docker_command("volume", "ls", "-q", "--filter", f"label={label}")
        else:
            command = self.docker_command("network", "ls", "-q", "--filter", f"label={label}")
        result = self._run(command, runner=runner, timeout=30.0, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"failed to list exact-label {resource} resources")
        return [line.strip() for line in _text(result.stdout).splitlines() if line.strip()]

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
            local_errors: list[BaseException] = [authority_error]
            try:
                self.remove_runtime_paths()
            except BaseException as cleanup_error:
                local_errors.append(cleanup_error)
            raise BaseExceptionGroup(
                "Phase 10 cleanup authority lost; Docker survivors are unknown",
                local_errors,
            ) from authority_error

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
            try:
                self.assert_socket_unchanged()
            except BaseException as error:
                errors.append(error)
            try:
                self.remove_runtime_paths()
            except BaseException as error:
                errors.append(error)
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


def write_authority_lease(authority: ComposeAuthority, path: Path) -> None:
    """Atomically create a private, no-follow lease; never overwrite one."""
    if path.parent != Path("/tmp") or not path.is_absolute():
        raise ValueError("authority lease must live directly below /tmp")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError("authority lease path is a symlink")
        raise FileExistsError(f"authority lease already exists: {path}")
    payload = (json.dumps(_authority_record(authority), sort_keys=True) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lease_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"authority lease {field} is malformed")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"authority lease {field} must be absolute")
    return path


def read_authority_lease(path: Path, *, expected_repo: Path) -> ComposeAuthority:
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
        runtime_metadata = runtime_path.lstat()
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
    )


def lease_path_for(repo: Path) -> Path:
    """Return the per-worktree persistent lease name without creating it."""
    identity = hashlib.sha256(str(repo.resolve(strict=True)).encode()).hexdigest()[:16]
    return Path("/tmp") / f"jhin-p10-worktree-{identity}.json"


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


def execute_one_shot(
    authority: ComposeAuthority,
    *,
    scenario: LiveScenario,
    lease_path: Path | None = None,
    runner: CommandRunner = run_command,
    child_runner: CommandRunner = run_live_child_command,
) -> subprocess.CompletedProcess[Any]:
    """Own stack→child-pytest→exhaustive-cleanup as one signal-safe lifecycle."""
    lease = (
        lease_path
        if lease_path is not None
        else Path("/tmp") / f"jhin-p10-live-{authority.token}.json"
    )
    if lease.parent != Path("/tmp") or not lease.is_absolute():
        raise ValueError("one-shot authority lease must live directly below /tmp")

    live_authority = authority
    cleaned = False
    cleaning = False
    cleanup_errors: list[BaseException] = []

    def cleanup() -> None:
        nonlocal cleaned, cleaning
        if cleaned or cleaning:
            return
        cleaning = True
        try:
            try:
                live_authority.down_and_cleanup(runner=runner, upgrade=scenario.upgrade)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                lease.unlink(missing_ok=True)
            except BaseException as error:
                cleanup_errors.append(error)
        finally:
            cleaned = True
            cleaning = False

    previous_handlers: dict[int, Any] = {}
    interrupted_signum: int | None = None

    def handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted_signum
        del frame
        if interrupted_signum is not None:
            return
        interrupted_signum = signum
        for caught_signal in (signal.SIGINT, signal.SIGTERM):
            signal.signal(caught_signal, signal.SIG_IGN)
        raise _LifecycleSignal(signum)

    atexit.register(cleanup)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except ValueError:
            # Signal handlers are process-main-thread infrastructure. Tests may
            # exercise this function from a worker thread without installing one.
            previous_handlers.clear()
            break

    primary_error: BaseException | None = None
    result: subprocess.CompletedProcess[Any] | None = None
    try:
        scenario_name = next(
            (name for name, candidate in LIVE_SCENARIOS.items() if candidate == scenario),
            "custom",
        )
        if scenario.start_stack:
            ports = live_authority.start_stack(runner=runner)
            live_authority = live_authority.with_published_ports(ports)
            if scenario.upgrade:
                source_ref = read_phase9_source_ref(live_authority.repo)
                frozen = live_authority.build_phase9_agent_image(source_ref, runner=runner)
                try:
                    live_authority = live_authority.with_upgrade_runtime(frozen)
                except BaseException as setup_error:
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
        write_authority_lease(live_authority, lease)
        result = child_runner(
            build_live_pytest_command(scenario),
            env=child_environment,
            cwd=live_authority.repo,
            timeout=5400.0,
            check=True,
            input_bytes=None,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
    except BaseException as error:
        primary_error = error
    finally:
        for signal_number in previous_handlers:
            signal.signal(signal_number, signal.SIG_IGN)
        child_group_survived = _contains_owned_process_group_survivor(primary_error)
        if not child_group_survived:
            cleanup()
        # If bounded SIGKILL could not prove the child group absent, even
        # local lease teardown is withheld: descendants may still be using
        # that authority and no cleanup may race them.
        atexit.unregister(cleanup)
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)

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


def _persistent_up(
    *,
    repo: Path,
    mode: str,
    runner: CommandRunner = run_command,
) -> ComposeAuthority:
    lease = lease_path_for(repo)
    if lease.exists() or lease.is_symlink():
        raise FileExistsError(f"persistent Phase 10 lease already exists: {lease}")
    authority = select_live_authority(repo=repo, mode=mode)
    try:
        ports = authority.start_stack(runner=runner)
        authority = authority.with_published_ports(ports)
        write_authority_lease(authority, lease)
        return authority
    except BaseException as primary:
        cleanup_error: BaseException | None = None
        try:
            authority.down_and_cleanup(runner=runner)
        except BaseException as error:
            cleanup_error = error
        lease.unlink(missing_ok=True)
        if cleanup_error is not None:
            raise BaseExceptionGroup(
                "persistent Phase 10 start and cleanup both failed",
                [primary, cleanup_error],
            ) from primary
        raise


def _load_persistent(repo: Path) -> tuple[Path, ComposeAuthority]:
    lease = lease_path_for(repo)
    return lease, read_authority_lease(lease, expected_repo=repo)


def _persistent_down(
    *,
    repo: Path,
    runner: CommandRunner = run_command,
) -> None:
    lease, authority = _load_persistent(repo)
    try:
        authority.assert_socket_unchanged()
        authority.down_and_cleanup(runner=runner)
    finally:
        lease.unlink(missing_ok=True)


def _persistent_compose(
    *,
    repo: Path,
    arguments: Sequence[str],
    runner: CommandRunner = run_command,
) -> subprocess.CompletedProcess[Any]:
    _lease, authority = _load_persistent(repo)
    authority.assert_socket_unchanged()
    selected_arguments = validate_persistent_compose_arguments(arguments)
    return authority._run(
        authority.compose_command(*selected_arguments),
        runner=runner,
        timeout=1200.0,
    )


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
        authority = select_live_authority(repo=repo, mode=cast(str, args.mode))
        execute_one_shot(authority, scenario=LIVE_SCENARIOS[cast(str, args.scenario)])
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
