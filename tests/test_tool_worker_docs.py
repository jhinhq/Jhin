"""Executable documentation contracts for the Phase 10 tool-worker boundary."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
BOUNDARY_DOC = ROOT / "docs" / "architecture" / "tool-worker-boundary.md"
SANDBOX_DOC = ROOT / "docs" / "architecture" / "sandboxing.md"
ENV_EXAMPLE = ROOT / ".env.example"

TARGET_SENSITIVE_ENV = (
    "APP_ENV",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_REMOVE_ORPHANS",
    "COMPOSE_IGNORE_ORPHANS",
    "COMPOSE_ENV_FILES",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
    "DOCKER_DEFAULT_PLATFORM",
)


@dataclass(frozen=True)
class ComposeCommand:
    files: tuple[str, ...]
    arguments: tuple[str, ...]


class AuditEvent(TypedDict):
    program: str
    args: list[str]
    env: dict[str, str | None]


def fenced_command_after(document: str, heading: str) -> str:
    """Return the first bash fence after an exact Markdown heading."""
    marker = f"{heading}\n"
    assert marker in document, f"missing heading: {heading}"
    tail = document.split(marker, maxsplit=1)[1]
    assert "```bash" in tail, f"missing bash command after: {heading}"
    return tail.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]


def compose_commands(command: str) -> tuple[ComposeCommand, ...]:
    """Parse literal docker-compose invocations from one operator fence."""
    logical = re.sub(r"\\\s*\n\s*", " ", command)
    parsed: list[ComposeCommand] = []
    for source_line in logical.splitlines():
        if "docker compose" not in source_line:
            continue
        tokens = shlex.split(source_line.strip())
        docker_index = tokens.index("docker")
        assert tokens[docker_index : docker_index + 2] == ["docker", "compose"]
        cursor = docker_index + 2
        files: list[str] = []
        while cursor < len(tokens) and tokens[cursor] == "-f":
            assert cursor + 1 < len(tokens), source_line
            files.append(tokens[cursor + 1])
            cursor += 2
        parsed.append(
            ComposeCommand(
                files=tuple(files),
                arguments=tuple(tokens[cursor:]),
            )
        )
    return tuple(parsed)


def _assert_exact_mode_commands(
    commands: tuple[ComposeCommand, ...],
    *,
    overlay: str,
) -> None:
    assert commands, "the operator fence must contain literal Compose commands"
    expected_files = ("compose.yaml", overlay)
    assert all(command.files == expected_files for command in commands)
    assert all(
        set(command.files).isdisjoint({"compose.rootless.yaml", "compose.rootful.yaml"} - {overlay})
        for command in commands
    )

    lifecycle = tuple(_compose_lifecycle_phase(command) for command in commands)
    expected = (
        (
            "runner-build",
            "sandbox-image-build",
            "up",
            "adapter-ping",
            "runner-health",
            "migrate",
            "ps",
        )
        if overlay == "compose.rootless.yaml"
        else ("sandbox-image-build", "up", "runner-health", "migrate", "ps")
    )
    assert lifecycle == expected


def _compose_lifecycle_phase(command: ComposeCommand) -> str:
    arguments = command.arguments
    if arguments == ("build", "sandbox-runner"):
        return "runner-build"
    if arguments == ("--profile", "build", "build", "sandbox-image"):
        return "sandbox-image-build"
    if arguments == ("up", "-d", "--build", "--wait", "--wait-timeout", "180"):
        return "up"
    if arguments[:3] == ("exec", "-T", "rootless-docker-transport"):
        assert arguments[:5] == ("exec", "-T", "rootless-docker-transport", "python", "-c")
        assert len(arguments) == 6 and "/_ping" in arguments[-1]
        return "adapter-ping"
    if arguments[:3] == ("exec", "-T", "sandbox-runner"):
        assert arguments[:5] == ("exec", "-T", "sandbox-runner", "python", "-c")
        assert len(arguments) == 6 and "127.0.0.1:8085/health" in arguments[-1]
        return "runner-health"
    if arguments == ("run", "--rm", "--no-deps", "api", "jhin-db-migrate"):
        return "migrate"
    if arguments == ("ps", "--all"):
        return "ps"
    raise AssertionError(f"unexpected Compose command: {arguments!r}")


def _write_audit_stub(path: Path, program: str) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys

names = os.environ["JHIN_DOC_AUDIT_NAMES"].split(",")
event = {{
    "program": {program!r},
    "args": sys.argv[1:],
    "env": {{name: os.environ.get(name) for name in names}},
}}
with open(os.environ["JHIN_DOC_AUDIT_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(event) + "\\n")
if {program!r} == "docker" and sys.argv[1:2] == ["--host"]:
    print('["name=rootless"]')
if {program!r} == "python" and "/var/run/docker.sock" in sys.argv[1:]:
    print("4321")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _execute_fence_with_poisoned_environment(
    command: str, tmp_path: Path
) -> tuple[AuditEvent, ...]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for program in ("docker", "python", "uv"):
        _write_audit_stub(binary_dir / program, program)

    audit_log = tmp_path / "audit.jsonl"
    environment = os.environ.copy()
    environment.update({name: f"poison-{name.casefold()}" for name in TARGET_SENSITIVE_ENV})
    environment["COMPOSE_DISABLE_ENV_FILE"] = "poison-disable-env-file"
    environment["JHIN_DOC_AUDIT_LOG"] = str(audit_log)
    environment["JHIN_DOC_AUDIT_NAMES"] = ",".join(
        (*TARGET_SENSITIVE_ENV, "COMPOSE_DISABLE_ENV_FILE", "PHASE10_SOCKET_MODE")
    )
    environment["PATH"] = f"{binary_dir}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        ["bash"],
        input=command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return tuple(
        cast(AuditEvent, json.loads(line))
        for line in audit_log.read_text(encoding="utf-8").splitlines()
    )


def _executed_lifecycle(events: tuple[AuditEvent, ...]) -> tuple[str, ...]:
    lifecycle: list[str] = []
    for event in events:
        if event["program"] == "python":
            lifecycle.append("socket-preflight")
        elif event["program"] == "uv":
            lifecycle.append("compose-assertion")
        elif event["args"][:1] == ["--host"]:
            lifecycle.append("daemon-preflight")
        else:
            parsed = compose_commands(f"docker {shlex.join(event['args'])}")
            assert len(parsed) == 1
            lifecycle.append(_compose_lifecycle_phase(parsed[0]))
    return tuple(lifecycle)


def test_documented_socket_commands_are_executable_and_mutually_exclusive() -> None:
    readme = README.read_text(encoding="utf-8")
    rootless = fenced_command_after(readme, "### Rootless Docker socket (Linux)")
    rootful = fenced_command_after(readme, "### Rootful Docker socket (Linux)")

    rootless_commands = compose_commands(rootless)
    rootful_commands = compose_commands(rootful)
    _assert_exact_mode_commands(rootless_commands, overlay="compose.rootless.yaml")
    _assert_exact_mode_commands(rootful_commands, overlay="compose.rootful.yaml")

    assert rootless_commands[0].arguments == ("build", "sandbox-runner")
    assert next(
        index
        for index, command in enumerate(rootless_commands)
        if command.arguments[:1] == ("build",)
    ) < next(
        index for index, command in enumerate(rootless_commands) if command.arguments[:1] == ("up",)
    )

    assert "COMPOSE_DISABLE_ENV_FILE=1" in rootless
    assert rootless.lstrip().startswith("set -euo pipefail")
    assert "PHASE10_SOCKET_MODE=rootless" in rootless
    assert "PHASE10_ROOTLESS_DOCKER_SOCKET" in rootless
    assert "SANDBOX_DOCKER_GID" not in rootless
    assert "lstat" in rootless and "stat.S_ISSOCK" in rootless
    assert "st_uid != 10001" in rootless
    assert "name=rootless" in rootless
    assert "assert_phase10_tool_worker_compose.py --mode rootless" in rootless
    assert 'export DOCKER_HOST="unix://$PHASE10_ROOTLESS_DOCKER_SOCKET"' in rootless
    assert rootless.index("name=rootless") < rootless.index("export DOCKER_HOST=")
    assert rootless.index("/_ping") < rootless.index("127.0.0.1:8085/health")

    assert "COMPOSE_DISABLE_ENV_FILE=1" in rootful
    assert rootful.lstrip().startswith("set -euo pipefail")
    assert "PHASE10_SOCKET_MODE=rootful" in rootful
    assert "SANDBOX_DOCKER_SOCKET_HOST" in rootful
    assert "SANDBOX_DOCKER_GID" in rootful
    assert "PHASE10_ROOTLESS_DOCKER_SOCKET" not in rootful
    assert "lstat" in rootful and "stat.S_ISSOCK" in rootful
    assert "is_absolute()" in rootful
    assert "stat.S_ISLNK" in rootful
    assert "st_uid != 0" in rootful
    assert "st_gid <= 0" in rootful
    assert "print(info.st_gid)" in rootful
    assert "assert_phase10_tool_worker_compose.py --mode rootful" in rootful
    assert 'export SANDBOX_DOCKER_GID="$(' not in rootful
    assert 'export DOCKER_HOST="unix://$SANDBOX_DOCKER_SOCKET_HOST"' in rootful
    assert rootful.index("export SANDBOX_DOCKER_GID") < rootful.index("export DOCKER_HOST=")

    command_text = f"{rootless}\n{rootful}".casefold()
    assert "app_env=production" not in command_text
    for unsafe in ("chmod", "chown", "sudo", "--privileged", "user: 0:0"):
        assert unsafe not in command_text


@pytest.mark.parametrize(
    ("heading", "mode", "socket_url"),
    (
        (
            "### Rootless Docker socket (Linux)",
            "rootless",
            "unix:///run/user/10001/docker.sock",
        ),
        (
            "### Rootful Docker socket (Linux)",
            "rootful",
            "unix:///var/run/docker.sock",
        ),
    ),
)
def test_documented_socket_fences_scrub_poisoned_environment_and_execute_exact_lifecycle(
    heading: str,
    mode: str,
    socket_url: str,
    tmp_path: Path,
) -> None:
    command = fenced_command_after(README.read_text(encoding="utf-8"), heading)
    events = _execute_fence_with_poisoned_environment(command, tmp_path)

    assertion_index = next(index for index, event in enumerate(events) if event["program"] == "uv")
    for event in events:
        assert event["env"]["COMPOSE_DISABLE_ENV_FILE"] == "1"
        assert event["env"]["COMPOSE_PROJECT_NAME"] == "jhin"
        assert event["env"]["PHASE10_SOCKET_MODE"] == mode
        for name in TARGET_SENSITIVE_ENV:
            if name not in {"COMPOSE_PROJECT_NAME", "DOCKER_HOST"}:
                assert event["env"][name] is None

    assert all(event["env"]["DOCKER_HOST"] is None for event in events[:assertion_index])
    assert all(event["env"]["DOCKER_HOST"] == socket_url for event in events[assertion_index:])

    assertion = events[assertion_index]
    assert assertion["args"] == [
        "run",
        "python",
        "scripts/assert_phase10_tool_worker_compose.py",
        "--mode",
        mode,
    ]
    if mode == "rootless":
        daemon_preflight = events[1]
        assert daemon_preflight["args"][:3] == ["--host", socket_url, "info"]
        assert daemon_preflight["env"]["DOCKER_HOST"] is None
    else:
        assert events[0]["args"] == ["-", "/var/run/docker.sock"]
    expected_lifecycle = (
        (
            "socket-preflight",
            "daemon-preflight",
            "compose-assertion",
            "runner-build",
            "sandbox-image-build",
            "up",
            "adapter-ping",
            "runner-health",
            "migrate",
            "ps",
        )
        if mode == "rootless"
        else (
            "socket-preflight",
            "compose-assertion",
            "sandbox-image-build",
            "up",
            "runner-health",
            "migrate",
            "ps",
        )
    )
    assert _executed_lifecycle(events) == expected_lifecycle


def test_boundary_document_binds_ownership_ids_and_compatibility_lifetime() -> None:
    document = BOUNDARY_DOC.read_text(encoding="utf-8")

    for required in (
        "resolve advertised tools → reason/bind → ordered execute → commit",
        "jhin-tool-queue",
        "phase10-tool-worker-boundary-v1",
        "phase10-trigger-sync-tool-routing-v1",
        "phase10-engineering-sync-tool-routing-v1",
        "phase10-compat-advertised-{run_id}-{step_index}",
        "phase10-compat-tool-step-{run_id}-{step_index}",
        "phase10-compat-approval-{approval_id}",
        "phase10-compat-sync-{run_id}",
        "phase10-compat-cleanup-{run_id}",
        "agent.step.tool_manifest",
        "agent.step.reasoning",
        "workflow.deprecate_patch",
        "execution_unknown",
    ):
        assert required in document

    assert "API payload is always `{}`" in document
    assert "ordinal`, `lossless`, `tool_name`, and `arguments_json" in document
    assert "completion, usage, transitions" in document
    assert "query all open histories" in document
    assert "no closed pre-patch history is queryable" in document


def test_boundary_document_records_the_exact_crash_outcome_matrix() -> None:
    document = BOUNDARY_DOC.read_text(encoding="utf-8")
    expected_rows = {
        "phase10.agent.before_manifest_bind.v1": "reruns the model; no tool effect",
        "phase9.agent.after_manifest.before_effect.v1": "reuses the committed pair",
        "phase10.tool.before_claim.v1": "executes once after recovery",
        "phase10.tool.after_claim.before_effect.v1": "execution_unknown",
        "phase10.tool.after_effect.before_terminal_commit.v1": "execution_unknown",
    }
    for barrier, outcome in expected_rows.items():
        row = next(line for line in document.splitlines() if barrier in line)
        assert outcome in row


def test_sandbox_document_describes_the_implemented_authority_boundary() -> None:
    document = SANDBOX_DOC.read_text(encoding="utf-8")
    for required in (
        "tool-worker → sandbox-runner",
        "10001:10001",
        "rootless-docker-transport",
        "0:0",
        "1000:1000",
        "pull_policy: never",
        "GET /_ping",
        "restart: unless-stopped",
        "503",
        "ps --all",
    ):
        assert required in document
    assert "agent-worker → sandbox-runner" not in document
    assert "rootful mode has no daemon-service dependency" in document
    assert "no socket, adapter endpoint or DNS, engine or runner network" in document
    assert "no supplemental group" in document


def test_environment_example_keeps_mode_values_inert_and_mode_specific() -> None:
    document = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "\nAPP_ENV=production\n" not in document
    for assignment in (
        "SANDBOX_DOCKER_SOCKET_HOST=",
        "SANDBOX_DOCKER_GID=",
        "PHASE10_ROOTLESS_DOCKER_SOCKET=",
    ):
        matching = [line for line in document.splitlines() if assignment in line]
        assert len(matching) == 1
        assert matching[0].startswith("# ")
    assert "COMPOSE_DISABLE_ENV_FILE=1" in document
