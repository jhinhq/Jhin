"""CLI tool executors (plan 11.6, 14.5): every tool is one sandbox job.

Execution path: the gateway has already authorized the call (capability +
scope: connection, command pattern, image, network, repository, path). Here
the job is submitted to the sandbox runner over the internal API, polled to
completion, and recorded:

- one ``sandbox_job`` row per job, linked to run/task/tool_call, written in
  the same transaction as the tool_call row;
- append-only audit events ``sandbox.job.started`` / ``completed`` /
  ``failed``;
- stdout/stderr redacted (runner-side against the job's secret env, worker-
  side against the process redactor) and size-capped before persistence.

Workspace persistence (documented in docs/architecture/sandboxing.md): all
jobs of one agent run share the named volume ``run-<run_id>`` mounted at
``/workspace``, so a checkout survives across tool calls; the volume is
destroyed when the run finalizes. Repository checkouts land in
``/workspace/repo`` and command-style jobs start there when it exists.

Git credentials (plan 13.6, 14.5): checkout resolves a short-lived token
from the referenced GitHub connection and injects it as job-scoped secret
env (``GIT_TOKEN``) consumed by an askpass helper — the token is never
embedded in the remote URL and never persisted in the workspace volume.
``cli.command.execute`` re-injects it on internet-networked jobs so
``git push`` works without the secret ever being written to disk.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from jhin_connectors.cli.runner_client import SandboxRunnerError, run_sandbox_job
from jhin_connectors.cli.schemas import (
    CommandExecuteInput,
    CommandExecuteOutput,
    FileReadInput,
    FileReadOutput,
    FileWriteInput,
    FileWriteOutput,
    RepositoryCheckoutInput,
    RepositoryCheckoutOutput,
    TestRunInput,
    TestRunOutput,
)
from jhin_connectors.execution import ConnectionResolutionError, resolve_connection
from jhin_connectors.github.auth import resolve_access_token
from jhin_connectors.github.client import DEFAULT_BASE_URL, validate_github_base_url
from jhin_db.models import AuditEvent, Connection, SandboxJob
from jhin_domain import ActorType, ConnectionStatus, SandboxJobStatus, new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_secrets.redaction import redact_text
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor

# Persisted/observed output tails; the runner caps raw capture much higher.
_MAX_TAIL_CHARS = 8_000
_MAX_FILE_CHARS = 6_000

_DEFAULT_COMMAND_TIMEOUT = 300
_DEFAULT_FILE_TIMEOUT = 60

_ASKPASS_PATH = "/workspace/.jhin-askpass"
_REPO_PATH = "/workspace/repo"

# Written once by checkout; contains no secret — it echoes the job-scoped
# GIT_TOKEN env var, which exists only for the lifetime of one container.
_ASKPASS_SCRIPT = (
    f"cat > {_ASKPASS_PATH} <<'ASKPASS'\n"
    "#!/bin/sh\n"
    'case "$1" in\n'
    "  Username*) echo x-access-token ;;\n"
    '  *) echo "$GIT_TOKEN" ;;\n'
    "esac\n"
    "ASKPASS\n"
    f"chmod +x {_ASKPASS_PATH}\n"
)


class CliToolError(Exception):
    """Tool-level failure with a message safe for models and persistence."""


async def _load_cli_connection(ctx: ToolExecutionContext, connection_id: str) -> Connection:
    """The CLI connection carries no credential, so it is loaded without
    decryption — but with the same workspace isolation and status checks as
    :func:`resolve_connection` (plan 48.4)."""
    try:
        target = UUID(connection_id)
    except ValueError:
        raise ConnectionResolutionError("connection_id is not a valid UUID") from None
    connection = await ctx.session.scalar(
        select(Connection).where(
            Connection.id == target,
            Connection.workspace_id == ctx.workspace_id,
            Connection.connector_type == "cli",
        )
    )
    if connection is None:
        raise ConnectionResolutionError(f"no cli connection {target} in this workspace")
    if connection.status == ConnectionStatus.DISABLED.value:
        raise ConnectionResolutionError(f"connection '{connection.name}' is disabled")
    return connection


def _connection_defaults(connection: Connection) -> tuple[str, str, str]:
    """(default_image, default_network, git_connection_id) from config."""
    config = connection.config_json
    image = str(config.get("default_image") or "")
    network = str(config.get("default_network") or "none")
    git_connection_id = str(config.get("git_connection_id") or "")
    if network not in ("none", "internet"):
        network = "none"
    return image, network, git_connection_id


async def _git_credentials(ctx: ToolExecutionContext, git_connection_id: str) -> tuple[str, str]:
    """(git_base_url, short-lived token) from the referenced GitHub
    connection — the plan-13.6 sandbox credential path."""
    if not git_connection_id:
        raise CliToolError(
            "no GitHub connection configured: set git_connection_id on the CLI "
            "connection or pass it in the tool call"
        )
    resolved = await resolve_connection(ctx, git_connection_id, connector_type="github")
    api_base = validate_github_base_url(str(resolved.config.get("base_url") or DEFAULT_BASE_URL))
    token = await resolve_access_token(
        resolved.connection.auth_type, resolved.credentials, api_base
    )
    # Real GitHub serves git on github.com; test/self-hosted layouts serve
    # git smart-HTTP under /git on the same server as the REST API.
    git_base = "https://github.com" if api_base == DEFAULT_BASE_URL else f"{api_base}/git"
    return git_base, token


def _workspace_key(ctx: ToolExecutionContext) -> str:
    return f"run-{ctx.run_id}"


def _tail(value: str) -> str:
    """Worker-side redaction pass + size cap before anything persists."""
    redacted = redact_text(value)
    if len(redacted) > _MAX_TAIL_CHARS:
        return redacted[-_MAX_TAIL_CHARS:]
    return redacted


async def _run_job(
    ctx: ToolExecutionContext,
    *,
    command_display: str,
    argv: list[str],
    image: str,
    network: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    secret_env: dict[str, str] | None = None,
) -> tuple[SandboxJob, dict[str, Any]]:
    """Submit one sandbox job, poll it to a terminal state, and persist the
    ``sandbox_job`` row + audit trail (plan 14, 23)."""
    row = SandboxJob(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        tool_call_id=ctx.tool_call_id,
        status=SandboxJobStatus.RUNNING.value,
        image=image or "(runner default)",
        command=redact_text(command_display)[:2_000],
        network_policy=network,
        timeout_seconds=timeout_seconds,
        started_at=datetime.now(UTC),
    )
    ctx.session.add(row)

    def audit(action: str, metadata: dict[str, Any]) -> None:
        ctx.session.add(
            AuditEvent(
                workspace_id=ctx.workspace_id,
                actor_type=ActorType.AGENT.value,
                actor_id=ctx.agent_id,
                action=action,
                target_type="sandbox_job",
                target_id=row.id,
                metadata_json={
                    "run_id": str(ctx.run_id),
                    "tool_call_id": str(ctx.tool_call_id) if ctx.tool_call_id else None,
                    "image": row.image,
                    "network_policy": network,
                    **metadata,
                },
            )
        )

    audit("sandbox.job.started", {"timeout_seconds": timeout_seconds})
    await ctx.session.flush()

    payload: dict[str, Any] = {
        "job_id": str(row.id),
        "image": image,
        "command": argv,
        "workspace_key": _workspace_key(ctx),
        "working_dir": "/workspace",
        "env": env or {},
        "secret_env": secret_env or {},
        "network_policy": network,
        "timeout_seconds": timeout_seconds,
    }
    try:
        result = await run_sandbox_job(payload, job_timeout_seconds=timeout_seconds)
    except SandboxRunnerError as exc:
        row.status = SandboxJobStatus.FAILED.value
        row.completed_at = datetime.now(UTC)
        row.error_code = "runner_error"
        row.stderr_tail = _tail(str(exc))
        audit("sandbox.job.failed", {"error": str(exc)[:300]})
        raise CliToolError(f"sandbox job failed: {exc}") from exc

    status = str(result.get("status", SandboxJobStatus.FAILED.value))
    row.status = status
    row.exit_code = cast("int | None", result.get("exit_code"))
    row.duration_ms = cast("int | None", result.get("duration_ms"))
    row.completed_at = datetime.now(UTC)
    row.stdout_tail = _tail(str(result.get("stdout", "")))
    row.stderr_tail = _tail(str(result.get("stderr", "")))
    if status != SandboxJobStatus.COMPLETED.value:
        row.error_code = status
        audit(
            "sandbox.job.failed",
            {"status": status, "error": str(result.get("error") or "")[:300]},
        )
    else:
        audit(
            "sandbox.job.completed",
            {"exit_code": row.exit_code, "duration_ms": row.duration_ms},
        )
    return row, result


def _job_output_fields(row: SandboxJob, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_job_id": str(row.id),
        "status": row.status,
        "exit_code": row.exit_code,
        "duration_ms": row.duration_ms,
        "stdout": row.stdout_tail,
        "stderr": row.stderr_tail,
        "stdout_truncated": bool(result.get("stdout_truncated", False)),
        "stderr_truncated": bool(result.get("stderr_truncated", False)),
    }


def _in_repo(script: str) -> str:
    """Command-style jobs start in the checkout when one exists."""
    return f"if [ -d {_REPO_PATH} ]; then cd {_REPO_PATH}; fi\n{script}"


# --- cli.command.execute ---


async def _command_execute(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(CommandExecuteInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, default_network, git_connection_id = _connection_defaults(connection)
    image = data.image or default_image
    network = data.network or default_network
    timeout = data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT

    env: dict[str, str] = {"HOME": "/workspace"}
    secret_env: dict[str, str] = {}
    if network == "internet" and git_connection_id:
        # git push needs the credential; injected per-job, never written to
        # the workspace (plan 13.6).
        try:
            _, token = await _git_credentials(ctx, git_connection_id)
            secret_env["GIT_TOKEN"] = token
            env["GIT_ASKPASS"] = _ASKPASS_PATH
        except (CliToolError, ConnectionResolutionError):
            pass  # command may not need git at all; run without credentials

    row, result = await _run_job(
        ctx,
        command_display=data.command,
        argv=["bash", "-c", _in_repo(data.command)],
        image=image,
        network=network,
        timeout_seconds=timeout,
        env=env,
        secret_env=secret_env,
    )
    return CommandExecuteOutput(command=data.command, **_job_output_fields(row, result))


# --- cli.repository.checkout ---


def _slug(repository: str) -> str:
    name = repository.split("/", 1)[-1].lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")
    return cleaned[:40] or "repo"


async def _repository_checkout(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RepositoryCheckoutInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, configured_git_id = _connection_defaults(connection)
    git_base, token = await _git_credentials(ctx, data.git_connection_id or configured_git_id)
    branch = data.branch or f"agent/{str(ctx.task_id)[:8]}-{_slug(data.repository)}"
    clone_url = f"{git_base}/{data.repository}.git"

    ref_arg = f"--branch {shlex.quote(data.ref)} " if data.ref else ""
    script = (
        "set -e\n"
        "export HOME=/workspace\n"
        f"{_ASKPASS_SCRIPT}"
        f"export GIT_ASKPASS={_ASKPASS_PATH}\n"
        f"rm -rf {_REPO_PATH}\n"
        f"git clone {ref_arg}{shlex.quote(clone_url)} {_REPO_PATH}\n"
        f"cd {_REPO_PATH}\n"
        'git config user.name "Jhin Agent"\n'
        'git config user.email "agent@jhin.local"\n'
        f"git checkout -b {shlex.quote(branch)}\n"
        'echo "JHIN_HEAD=$(git rev-parse HEAD)"\n'
    )
    row, result = await _run_job(
        ctx,
        command_display=f"git clone {clone_url} && git checkout -b {branch}",
        argv=["bash", "-c", script],
        image=data.image or default_image,
        network="internet",  # clone always needs the sandbox bridge
        timeout_seconds=data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT,
        secret_env={"GIT_TOKEN": token},
    )

    head_sha = ""
    for line in row.stdout_tail.splitlines():
        if line.startswith("JHIN_HEAD="):
            head_sha = line.removeprefix("JHIN_HEAD=").strip()
    return RepositoryCheckoutOutput(
        repository=data.repository,
        branch=branch,
        head_sha=head_sha,
        path=_REPO_PATH,
        **_job_output_fields(row, result),
    )


# --- cli.test.run ---


async def _test_run(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(TestRunInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, default_network, _ = _connection_defaults(connection)
    row, result = await _run_job(
        ctx,
        command_display=data.command,
        argv=["bash", "-c", _in_repo(data.command)],
        image=data.image or default_image,
        network=data.network or default_network,
        timeout_seconds=data.timeout_seconds or _DEFAULT_COMMAND_TIMEOUT,
        env={"HOME": "/workspace"},
    )
    return TestRunOutput(
        command=data.command,
        passed=row.exit_code == 0 and row.status == SandboxJobStatus.COMPLETED.value,
        **_job_output_fields(row, result),
    )


# --- cli.file.read ---


async def _file_read(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileReadInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    script = _in_repo(f"cat -- {shlex.quote(data.path)}")
    row, result = await _run_job(
        ctx,
        command_display=f"cat {data.path}",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",  # file reads never need egress
        timeout_seconds=_DEFAULT_FILE_TIMEOUT,
    )
    if row.status != SandboxJobStatus.COMPLETED.value or row.exit_code != 0:
        raise CliToolError(
            f"file read failed ({row.status}, exit {row.exit_code}): {row.stderr_tail[:300]}"
        )
    content = row.stdout_tail
    truncated = bool(result.get("stdout_truncated", False)) or len(content) > _MAX_FILE_CHARS
    return FileReadOutput(
        sandbox_job_id=str(row.id),
        path=data.path,
        content=content[:_MAX_FILE_CHARS],
        truncated=truncated,
    )


# --- cli.file.write ---


async def _file_write(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(FileWriteInput, payload)
    connection = await _load_cli_connection(ctx, data.connection_id)
    default_image, _, _ = _connection_defaults(connection)
    quoted = shlex.quote(data.path)
    script = _in_repo(
        "set -e\n"
        f'mkdir -p "$(dirname -- {quoted})"\n'
        f"printf '%s' \"$JHIN_FILE_CONTENT\" > {quoted}\n"
        f"wc -c < {quoted}\n"
    )
    row, _ = await _run_job(
        ctx,
        command_display=f"write {data.path} ({len(data.content)} chars)",
        argv=["bash", "-c", script],
        image=default_image,
        network="none",  # file writes never need egress
        timeout_seconds=_DEFAULT_FILE_TIMEOUT,
        env={"JHIN_FILE_CONTENT": data.content},
    )
    if row.status != SandboxJobStatus.COMPLETED.value or row.exit_code != 0:
        raise CliToolError(
            f"file write failed ({row.status}, exit {row.exit_code}): {row.stderr_tail[:300]}"
        )
    try:
        bytes_written = int(row.stdout_tail.strip().splitlines()[-1])
    except (ValueError, IndexError):
        bytes_written = len(data.content.encode())
    return FileWriteOutput(sandbox_job_id=str(row.id), path=data.path, bytes_written=bytes_written)


CLI_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="cli.command.execute",
            description=(
                "Run a shell command inside an ephemeral sandbox container. The "
                "workspace (and any repository checkout at /workspace/repo) "
                "persists across calls within one run. Use it to commit and push "
                "sandbox edits: `git add -A && git commit -m '...' && git push -u "
                "origin HEAD` with network='internet' (the checkout's credential "
                "is injected automatically)."
            ),
            risk=RiskLevel.WRITE,
            input_model=CommandExecuteInput,
            output_model=CommandExecuteOutput,
            required_capability="cli.command.execute",
            supports_approval=True,
            scope_keys=("connection_id", "command", "image", "network"),
        ),
        _command_execute,
    ),
    (
        ToolDefinition(
            name="cli.repository.checkout",
            description=(
                "Clone a repository into the sandbox workspace using a short-lived "
                "credential and create a working branch (default: agent/<task>-<repo>). "
                "Edit files with cli.file.write, then commit and push that branch with "
                "cli.command.execute (network='internet') and open the pull request "
                "from it; do not create the branch through the GitHub API, that would "
                "give the pull request no changes."
            ),
            risk=RiskLevel.WRITE,
            input_model=RepositoryCheckoutInput,
            output_model=RepositoryCheckoutOutput,
            required_capability="cli.repository.checkout",
            supports_approval=True,
            scope_keys=("connection_id", "repository", "image"),
        ),
        _repository_checkout,
    ),
    (
        ToolDefinition(
            name="cli.test.run",
            description=(
                "Run a test command in the sandbox workspace and report pass/fail with output."
            ),
            risk=RiskLevel.READ,
            input_model=TestRunInput,
            output_model=TestRunOutput,
            required_capability="cli.test.run",
            scope_keys=("connection_id", "command", "image"),
        ),
        _test_run,
    ),
    (
        ToolDefinition(
            name="cli.file.read",
            description="Read one file from the sandbox workspace (path relative to the checkout).",
            risk=RiskLevel.READ,
            input_model=FileReadInput,
            output_model=FileReadOutput,
            required_capability="cli.file.read",
            scope_keys=("connection_id", "path"),
        ),
        _file_read,
    ),
    (
        ToolDefinition(
            name="cli.file.write",
            description=(
                "Write one file in the sandbox workspace (path relative to the checkout). "
                "The change exists only in the sandbox until you commit and push it with "
                "cli.command.execute (git add/commit/push, network='internet')."
            ),
            risk=RiskLevel.WRITE,
            input_model=FileWriteInput,
            output_model=FileWriteOutput,
            required_capability="cli.file.write",
            supports_approval=True,
            scope_keys=("connection_id", "path"),
        ),
        _file_write,
    ),
)
