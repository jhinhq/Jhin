"""Job execution engine: fresh ephemeral Docker container per job (plan 14).

Isolation invariants enforced here (plan 14.1, 14.3, 48.7):

- jobs NEVER receive the Docker socket, host mounts, or the compose
  control/data networks — the only mounts are a named workspace volume and
  a tmpfs;
- ``network_policy=none`` → Docker ``none`` network; ``internet`` → a
  dedicated sandbox bridge network that carries no control-plane services;
- read-only root filesystem, cap_drop ALL, no-new-privileges, non-root
  uid 1000, CPU/memory/pids caps, wall-clock timeout;
- containers are force-removed in a ``finally`` and labeled so orphans from
  a crashed runner are reaped at the next startup;
- captured stdout/stderr is redacted (job secret values) and size-capped
  before it ever leaves this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from jhin_observability import get_logger
from jhin_sandbox_runner.docker_socket import (
    DockerSocketConfigurationError,
    normalize_supplemental_groups,
    validate_docker_authority,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest, SandboxJobStatusResponse
from jhin_sandbox_runner.settings import Settings
from jhin_secrets.redaction import SecretRedactor

logger = get_logger(__name__)

JOB_LABEL = "jhin.sandbox.job"
WORKSPACE_LABEL = "jhin.sandbox.workspace"
WORKSPACE_VOLUME_PREFIX = "jhin-sandbox-ws-"

_TRUNCATION_MARKER = "\n…[truncated by sandbox runner]"
_POLL_INTERVAL_SECONDS = 0.5
DOCKER_CHECK_TIMEOUT_SECONDS = 5.0
_FORBIDDEN_JOB_ENV_NAME_PREFIXES = ("DOCKER_", "SANDBOX_DOCKER_")
_FORBIDDEN_JOB_ENV_SOCKET_PATHS = (
    "/var/run/docker.sock",
    "/run/jhin/docker.sock",
    "/run/host/docker.sock",
)
_ROOTLESS_TRANSPORT_HOSTNAME = "rootless-docker-transport"


class JobValidationError(Exception):
    """The request asks for more than the configured caps allow."""


class DockerDaemonConfigurationError(RuntimeError):
    """The selected Docker daemon does not match the configured trust mode."""


@dataclass
class JobRecord:
    request: SandboxJobRequest
    image: str
    cpu_limit: float
    memory_mb: int
    pids_limit: int
    timeout_seconds: int
    status: str = "running"
    container_id: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None
    redactor: SecretRedactor = field(default_factory=SecretRedactor)

    def to_response(self) -> SandboxJobStatusResponse:
        duration_ms: int | None = None
        if self.started_at is not None and self.finished_at is not None:
            duration_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)
        return SandboxJobStatusResponse(
            job_id=self.request.job_id,
            status=self.status,
            image=self.image,
            network_policy=self.request.network_policy,
            exit_code=self.exit_code,
            started_at=self.started_at.isoformat() if self.started_at else None,
            finished_at=self.finished_at.isoformat() if self.finished_at else None,
            duration_ms=duration_ms,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            error=self.error,
        )


def resolve_limits(request: SandboxJobRequest, settings: Settings) -> tuple[float, int, int, int]:
    """(cpu, memory_mb, pids, timeout) — request values bounded by hard caps.

    Requests above a cap are rejected loudly rather than silently clamped,
    so a misconfigured grant surfaces as an error instead of a surprise.
    """
    cpu = request.cpu_limit if request.cpu_limit is not None else settings.sandbox_max_cpus
    memory = request.memory_mb if request.memory_mb is not None else settings.sandbox_max_memory_mb
    pids = request.pids_limit if request.pids_limit is not None else settings.sandbox_max_pids
    timeout = (
        request.timeout_seconds
        if request.timeout_seconds is not None
        else settings.sandbox_max_timeout_seconds
    )
    if cpu > settings.sandbox_max_cpus:
        raise JobValidationError(f"cpu_limit {cpu} exceeds cap {settings.sandbox_max_cpus}")
    if memory > settings.sandbox_max_memory_mb:
        raise JobValidationError(f"memory_mb {memory} exceeds cap {settings.sandbox_max_memory_mb}")
    if pids > settings.sandbox_max_pids:
        raise JobValidationError(f"pids_limit {pids} exceeds cap {settings.sandbox_max_pids}")
    if timeout > settings.sandbox_max_timeout_seconds:
        raise JobValidationError(
            f"timeout_seconds {timeout} exceeds cap {settings.sandbox_max_timeout_seconds}"
        )
    return cpu, memory, pids, timeout


def workspace_volume_name(workspace_key: str) -> str:
    return f"{WORKSPACE_VOLUME_PREFIX}{workspace_key}"


def _job_environment_value_is_safe(name: str, value: str) -> bool:
    if name.startswith(_FORBIDDEN_JOB_ENV_NAME_PREFIXES):
        return False
    if _ROOTLESS_TRANSPORT_HOSTNAME in value.casefold():
        return False
    return not any(path in value for path in _FORBIDDEN_JOB_ENV_SOCKET_PATHS)


def build_container_config(
    request: SandboxJobRequest,
    settings: Settings,
    *,
    image: str,
    cpu_limit: float,
    memory_mb: int,
    pids_limit: int,
) -> dict[str, Any]:
    """The Docker container create payload for one job.

    Pure function so the security-relevant knobs are directly unit-testable
    (no privileged mode, no host network, cap drop, read-only root, ...).
    """
    requested_env = {**request.env, **request.secret_env}
    safe_env = {
        name: value
        for name, value in requested_env.items()
        if _job_environment_value_is_safe(name, value)
    }
    env = {
        # Read-only root: HOME must live on the writable workspace so tools
        # like git can write their config.
        "HOME": request.working_dir,
        **safe_env,
    }
    network_mode = "none" if request.network_policy == "none" else settings.sandbox_network
    if network_mode in {"runner", "engine"}:
        raise JobValidationError("jobs cannot join a control-plane network")
    host_config: dict[str, Any] = {
        "NetworkMode": network_mode,
        "Memory": memory_mb * 1024 * 1024,
        "MemorySwap": memory_mb * 1024 * 1024,  # no swap beyond the cap
        "NanoCpus": int(cpu_limit * 1_000_000_000),
        "PidsLimit": pids_limit,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
        "Privileged": False,
        "Tmpfs": {"/tmp": "rw,size=268435456,mode=1777"},
        "AutoRemove": False,
    }
    if request.workspace_key:
        host_config["Binds"] = [
            f"{workspace_volume_name(request.workspace_key)}:{request.working_dir}"
        ]
    else:
        # No persistent workspace requested: still give the job a writable
        # working directory that dies with the container.
        host_config["Tmpfs"][request.working_dir] = "rw,size=268435456,mode=1777"
    return {
        "Image": image,
        "Cmd": list(request.command),
        "Env": [f"{name}={value}" for name, value in env.items()],
        "WorkingDir": request.working_dir,
        "User": "1000:1000",
        "Labels": {JOB_LABEL: request.job_id},
        "HostConfig": host_config,
    }


class JobManager:
    """In-memory job registry + container lifecycle. Durable job records live
    in Postgres and are written by the caller — this registry only needs to
    survive for the duration of a job plus a polling grace period."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._docker: aiodocker.Docker | None = None
        self._jobs: dict[str, JobRecord] = {}

    @property
    def docker(self) -> aiodocker.Docker:
        assert self._docker is not None, "JobManager.start() not called"
        return self._docker

    async def start(self) -> None:
        effective_uid = os.geteuid()
        effective_gid = os.getegid()
        if self._settings.sandbox_docker_mode == "rootless" and effective_gid != 10001:
            raise DockerSocketConfigurationError("rootless runner requires UID/GID 10001:10001")
        authority_groups = normalize_supplemental_groups(
            effective_gid=effective_gid,
            process_groups=os.getgroups(),
        )
        if (
            self._settings.sandbox_docker_mode == "rootful"
            and self._settings.sandbox_docker_gid == effective_gid
        ):
            authority_groups.add(effective_gid)
        validated_url = validate_docker_authority(
            mode=self._settings.sandbox_docker_mode,
            socket_path=self._settings.sandbox_docker_socket,
            transport_url=self._settings.sandbox_docker_transport_url,
            configured_gid=self._settings.sandbox_docker_gid,
            effective_uid=effective_uid,
            supplemental_groups=authority_groups,
        )
        client = aiodocker.Docker(url=validated_url)
        self._docker = client
        try:
            await asyncio.wait_for(client.version(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
            info = await asyncio.wait_for(
                client.system.info(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS
            )
            security_options = info.get("SecurityOptions", [])
            if self._settings.sandbox_docker_mode == "rootless" and (
                not isinstance(security_options, list) or "name=rootless" not in security_options
            ):
                raise DockerDaemonConfigurationError(
                    "configured rootless Docker daemon is not rootless"
                )
            await self._ensure_sandbox_network()
            await self.reap_orphans()
        except BaseException:
            self._docker = None
            with contextlib.suppress(Exception):
                await client.close()
            raise

    async def _ensure_sandbox_network(self) -> None:
        """Create the dedicated job bridge network if compose has not.

        In production no compose service is attached to it (plan 14.4 — the
        sandbox network must carry no control-plane services), so the runner
        owns its creation.
        """
        name = self._settings.sandbox_network
        networks = await self.docker.networks.list(filters={"name": name})
        if not any(entry.get("Name") == name for entry in networks):
            await self.docker.networks.create(
                {"Name": name, "Driver": "bridge", "Labels": {"jhin.sandbox.network": "1"}}
            )
            logger.info("sandbox.network_created", network=name)

    async def close(self) -> None:
        for record in self._jobs.values():
            if record.task is not None and not record.task.done():
                record.task.cancel()
        if self._docker is not None:
            client = self._docker
            self._docker = None
            await client.close()

    async def ping(self) -> bool:
        try:

            async def ping_daemon() -> bool:
                async with self.docker._query("_ping", versioned_api=False) as response:
                    return response.status == 200 and await response.text() == "OK"

            return await asyncio.wait_for(ping_daemon(), timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
        except Exception:
            return False

    # --- lifecycle ---

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def submit(self, request: SandboxJobRequest) -> JobRecord:
        if request.job_id in self._jobs:
            raise JobValidationError(f"job {request.job_id} already exists")
        cpu, memory, pids, timeout = resolve_limits(request, self._settings)
        image = request.image or self._settings.sandbox_default_image
        record = JobRecord(
            request=request,
            image=image,
            cpu_limit=cpu,
            memory_mb=memory,
            pids_limit=pids,
            timeout_seconds=timeout,
        )
        for value in request.secret_env.values():
            record.redactor.register(value)
        self._jobs[request.job_id] = record
        record.task = asyncio.create_task(self._run(record))
        return record

    async def cancel(self, job_id: str) -> JobRecord | None:
        record = self._jobs.get(job_id)
        if record is None:
            return None
        record.cancel_requested = True
        if record.status == "running" and record.container_id is not None:
            try:
                container = self.docker.containers.container(record.container_id)
                await container.kill()
            except DockerError:
                pass  # already gone
        return record

    async def _run(self, record: JobRecord) -> None:
        request = record.request
        record.started_at = datetime.now(UTC)
        container = None
        timed_out = False
        terminal_status = "failed"
        try:
            if request.workspace_key:
                await self._ensure_workspace_volume(request.workspace_key)
            config = build_container_config(
                request,
                self._settings,
                image=record.image,
                cpu_limit=record.cpu_limit,
                memory_mb=record.memory_mb,
                pids_limit=record.pids_limit,
            )
            container = await self.docker.containers.create(
                config, name=f"jhin-sbx-{request.job_id[:32]}"
            )
            record.container_id = container.id
            await container.start()
            timed_out = await self._wait(record, container)
            info = await container.show()
            record.exit_code = int(info.get("State", {}).get("ExitCode", -1))
            await self._collect_logs(record, container)
            if record.cancel_requested:
                terminal_status = "cancelled"
            elif timed_out:
                terminal_status = "timeout"
                record.error = f"job exceeded its {record.timeout_seconds}s timeout and was killed"
            else:
                terminal_status = "completed"
        except DockerError as exc:
            record.error = record.redactor.redact_text(f"docker error: {exc.message}")[:2000]
        except Exception as exc:  # infrastructure failure — never secrets
            record.error = record.redactor.redact_text(f"{type(exc).__name__}: {exc}")[:2000]
        finally:
            # Ephemeral always (plan 14.1): the container is force-removed no
            # matter how the job ended.
            if container is not None:
                # Startup reaping is the backstop if this delete fails.
                with contextlib.suppress(DockerError):
                    await container.delete(force=True, v=True)
            record.finished_at = datetime.now(UTC)
            record.status = terminal_status
            logger.info(
                "sandbox.job.finished",
                job_id=request.job_id,
                status=record.status,
                exit_code=record.exit_code,
                image=record.image,
                network_policy=request.network_policy,
            )

    async def _wait(self, record: JobRecord, container: Any) -> bool:
        """Poll until exit/cancel/timeout. Returns True when timed out.

        Polling (instead of the blocking wait endpoint) keeps long jobs
        immune to HTTP client read timeouts and lets cancellation act fast.
        """
        assert record.started_at is not None
        while True:
            info = await container.show()
            if not info.get("State", {}).get("Running", False):
                return False
            elapsed = (datetime.now(UTC) - record.started_at).total_seconds()
            if elapsed >= record.timeout_seconds:
                with contextlib.suppress(DockerError):
                    await container.kill()
                return True
            if record.cancel_requested:
                with contextlib.suppress(DockerError):
                    await container.kill()
                return False
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _collect_logs(self, record: JobRecord, container: Any) -> None:
        max_bytes = self._settings.sandbox_max_output_bytes
        try:
            stdout = "".join(await container.log(stdout=True))
            stderr = "".join(await container.log(stderr=True))
        except DockerError:
            stdout, stderr = "", ""
        record.stdout, record.stdout_truncated = self._sanitize(record, stdout, max_bytes)
        record.stderr, record.stderr_truncated = self._sanitize(record, stderr, max_bytes)

    @staticmethod
    def _sanitize(record: JobRecord, text: str, max_bytes: int) -> tuple[str, bool]:
        redacted = record.redactor.redact_text(text)
        if len(redacted.encode()) <= max_bytes:
            return redacted, False
        # Keep the tail: for build/test output the end is what matters.
        clipped = redacted.encode()[-max_bytes:].decode(errors="ignore")
        return clipped + _TRUNCATION_MARKER, True

    async def current_logs(self, job_id: str) -> tuple[str, str, bool, bool] | None:
        """(stdout, stderr, stdout_truncated, stderr_truncated) — the stored
        output for finished jobs, a live (redacted, capped) read for running
        ones."""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if record.status != "running" or record.container_id is None:
            return record.stdout, record.stderr, record.stdout_truncated, record.stderr_truncated
        max_bytes = self._settings.sandbox_max_output_bytes
        try:
            container = self.docker.containers.container(record.container_id)
            stdout = "".join(await container.log(stdout=True))
            stderr = "".join(await container.log(stderr=True))
        except DockerError:
            stdout, stderr = "", ""
        out, out_truncated = self._sanitize(record, stdout, max_bytes)
        err, err_truncated = self._sanitize(record, stderr, max_bytes)
        return out, err, out_truncated, err_truncated

    # --- workspaces ---

    async def _ensure_workspace_volume(self, workspace_key: str) -> None:
        await self.docker.volumes.create(
            {
                "Name": workspace_volume_name(workspace_key),
                "Labels": {WORKSPACE_LABEL: workspace_key},
            }
        )

    async def delete_workspace(self, workspace_key: str) -> bool:
        name = workspace_volume_name(workspace_key)
        try:
            volume = await self.docker.volumes.get(name)
            await volume.delete()
            return True
        except DockerError:
            return False

    # --- startup reaping ---

    async def reap_orphans(self) -> None:
        """Remove leftover job containers (and stale workspace volumes) from
        a previous runner process that died mid-job."""
        containers = await self.docker.containers.list(
            all=1, filters=json.dumps({"label": [JOB_LABEL]})
        )
        for container in containers:
            await container.delete(force=True, v=True)
            logger.info("sandbox.reaped_container", container_id=container.id[:12])

        max_age = self._settings.sandbox_workspace_max_age_hours * 3600
        listing = await self.docker.volumes.list(filters={"label": [WORKSPACE_LABEL]})
        for entry in listing.get("Volumes") or []:
            created = entry.get("CreatedAt", "")
            age = (datetime.now(UTC) - datetime.fromisoformat(created)).total_seconds()
            if age > max_age:
                volume = await self.docker.volumes.get(entry["Name"])
                await volume.delete()
                logger.info("sandbox.reaped_workspace", volume=entry["Name"])
