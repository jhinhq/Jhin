"""Internal, bounded interactive sessions. Only the runner has Docker authority."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from jhin_sandbox_runner.jobs import JobManager, build_container_config, resolve_limits
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings
from jhin_secrets.redaction import get_redactor

OUTPUT_LIMIT = 131_072
SESSION_LIMIT = 16
INPUT_CLIENT_LIMIT = 1024
RETAIN_SECONDS = 3600
IDENTITY_LIMIT = 4096


class SessionRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9-]{16,64}$")
    workspace_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,81}$")
    kind: Literal["terminal", "preview"] = "terminal"
    network: Literal["none", "internet"] = "none"
    command: str = Field(default="", max_length=8000)
    port: int = Field(default=3000, ge=1024, le=65535)
    expires_in_seconds: int = Field(default=28800, ge=60, le=28800)
    image: str = Field(default="", max_length=300)
    cols: int = Field(default=100, ge=20, le=500)
    rows: int = Field(default=30, ge=5, le=200)


@dataclass
class InputSequence:
    last: int = 0

    def accept(self, sequence: int) -> bool:
        if sequence <= self.last:
            return False
        self.last = sequence
        return True


def session_container_config(request: SessionRequest, settings: Settings) -> dict[str, Any]:
    job = SandboxJobRequest(
        job_id=request.session_id,
        command=["sleep", "infinity"],
        workspace_key=request.workspace_key,
        network_policy=request.network,
    )
    cpu, memory, pids, _ = resolve_limits(job, settings)
    config = build_container_config(
        job,
        settings,
        image=request.image or settings.sandbox_default_image,
        cpu_limit=cpu,
        memory_mb=memory,
        pids_limit=pids,
    )
    config["Labels"]["jhin.session.kind"] = request.kind
    config["Env"].extend(["TERM=xterm-256color", "PYTHONUNBUFFERED=1"])
    if request.kind == "preview":
        # Preview workspace is an isolated copy, never the authoring volume.
        config["HostConfig"]["Mounts"][0]["ReadOnly"] = True
        # Native toolchain modules (SWC, Rolldown) need executable mappings.
        # Only disposable /app is executable; source stays read-only.
        config["HostConfig"]["Tmpfs"]["/app"] = "rw,exec,nosuid,nodev,size=1073741824,mode=1777"
    return config


@dataclass
class SessionRecord:
    request: SessionRequest
    container: Any
    process: Any = None
    stream: Any = None
    reader: asyncio.Task[None] | None = None
    status: str = "starting"
    output: str = ""
    pending_output: str = ""
    offset: int = 0
    exit_code: int | None = None
    workspace_size_bytes: int | None = None
    workspace_size_partial: bool = False
    workspace_size_measured_at: str | None = None
    last_input: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    sequences: dict[str, InputSequence] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def append(self, value: str, *, final: bool = False) -> None:
        text, self.pending_output = get_redactor().stream_chunk(
            self.pending_output + value, final=final
        )
        self.offset += len(text)
        self.output = (self.output + text)[-OUTPUT_LIMIT:]

    def snapshot(self, after: int | None = None) -> dict[str, Any]:
        start = self.offset - len(self.output)
        output = self.output if after is None else self.output[max(0, after - start) :]
        return {
            "id": self.request.session_id,
            "kind": self.request.kind,
            "status": self.status,
            "cwd": "/workspace",
            "network": self.request.network,
            "exit_code": self.exit_code,
            "output": output,
            "output_offset": self.offset,
            "output_start": start,
            "truncated": after is not None and after < start,
            "workspace_size_bytes": self.workspace_size_bytes,
            "workspace_size_partial": self.workspace_size_partial,
            "workspace_size_measured_at": self.workspace_size_measured_at,
        }


class SessionManager:
    def __init__(self, jobs: JobManager, settings: Settings):
        self.jobs = jobs
        self.settings = settings
        self.records: dict[str, SessionRecord] = {}
        self.retired_ids: set[str] = set()
        self.create_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task[None] | None = None

    async def cleanup(self) -> None:
        current = time.monotonic()
        for identity, record in list(self.records.items()):
            if record.status in {"starting", "running"}:
                if current - record.created_at >= record.request.expires_in_seconds:
                    await self.stop(identity)
                    record.status = "expired"
            elif record.finished_at and current - record.finished_at > RETAIN_SECONDS:
                self.retired_ids.add(identity)
                self.records.pop(identity, None)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await self.cleanup()

    async def create(self, request: SessionRequest) -> SessionRecord:
        async with self.create_lock:
            if self.cleanup_task is None:
                self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            await self.cleanup()
            return await self._create(request)

    async def _create(self, request: SessionRequest) -> SessionRecord:
        if request.session_id in self.retired_ids:
            raise HTTPException(410, "Session ended; create a new session identity")
        if len(self.records) + len(self.retired_ids) >= IDENTITY_LIMIT:
            raise HTTPException(429, "Session identity capacity reached; restart the runtime")
        existing = self.records.get(request.session_id)
        if existing:
            if existing.request != request:
                raise HTTPException(409, "session identity already exists")
            return existing
        if sum(r.status in {"starting", "running"} for r in self.records.values()) >= SESSION_LIMIT:
            raise HTTPException(429, "interactive session capacity reached")
        # The same exclusion table as command jobs, claimed before any await.
        holder = self.jobs._workspace_holders.get(request.workspace_key)
        if holder is not None:
            raise HTTPException(409, "workspace is busy")
        self.jobs._workspace_holders[request.workspace_key] = request.session_id
        container = None
        try:
            size, partial = await self.jobs._ensure_workspace_volume(
                request.workspace_key, job_id=request.session_id
            )
            container = await self.jobs.docker.containers.create(
                session_container_config(request, self.settings),
                name=f"jhin-session-{request.session_id}",
            )
            record = SessionRecord(request=request, container=container)
            record.workspace_size_bytes, record.workspace_size_partial = size, partial
            record.workspace_size_measured_at = datetime.now(UTC).isoformat()
            self.records[request.session_id] = record
            await container.start()
            command = ["bash", "--noprofile", "--norc"]
            if request.kind == "preview":
                command = [
                    "bash",
                    "-c",
                    "cp -R /workspace/. /app/ && cd /app && " + request.command,
                ]
            process = await container.exec(
                cmd=command, stdin=True, tty=True, user="1000:1000", workdir="/workspace"
            )
            record.process = process
            record.stream = process.start()
            # write_in opens the exec transport and starts the process.
            await record.stream.write_in(b"")
            await process.resize(h=request.rows, w=request.cols)
            record.status = "running"
            record.reader = asyncio.create_task(self._read(record))
            return record
        except BaseException:
            if container is not None:
                with contextlib.suppress(Exception):
                    await container.delete(force=True, v=True)
            self.jobs._workspace_holders.pop(request.workspace_key, None)
            self.records.pop(request.session_id, None)
            raise

    async def _read(self, record: SessionRecord) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while message := await record.stream.read_out():
                record.append(decoder.decode(message.data))
            record.append(decoder.decode(b"", final=True), final=True)
            info = await record.process.inspect()
            record.exit_code = info.get("ExitCode")
            if record.status != "stopping":
                record.status = "completed" if record.exit_code == 0 else "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            if record.status != "stopping":
                record.status = "failed"
        finally:
            if record.status not in {"running", "starting", "stopping"}:
                final_status = record.status
                record.status = "stopping"
                with contextlib.suppress(Exception):
                    await record.container.delete(force=True, v=True)
                await self._measure_terminal(record)
                record.status = final_status
                record.finished_at = time.monotonic()
                if (
                    self.jobs._workspace_holders.get(record.request.workspace_key)
                    == record.request.session_id
                ):
                    self.jobs._workspace_holders.pop(record.request.workspace_key, None)

    async def _measure_terminal(self, record: SessionRecord) -> None:
        if record.request.kind != "terminal":
            return
        try:
            size, partial = await self.jobs._ensure_workspace_volume(
                record.request.workspace_key, job_id=record.request.session_id
            )
        except Exception:
            size, partial = 0, True
        record.workspace_size_bytes = size
        record.workspace_size_partial = partial
        record.workspace_size_measured_at = datetime.now(UTC).isoformat()

    def get(self, session_id: str) -> SessionRecord:
        record = self.records.get(session_id)
        if record is None:
            raise HTTPException(404, "session no longer exists")
        return record

    async def input(self, session_id: str, message: dict[str, Any]) -> dict[str, Any]:
        record = self.get(session_id)
        if record.status != "running":
            raise HTTPException(409, "session is not running")
        kind = message.get("type")
        async with record.write_lock:
            if kind == "input":
                data = message.get("data")
                sequence = message.get("seq")
                if (
                    not isinstance(data, str)
                    or len(data) > 16_384
                    or type(sequence) is not int
                    or sequence < 1
                ):
                    raise HTTPException(422, "invalid terminal input")
                client_id = message.get("client_id", "internal")
                if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
                    raise HTTPException(422, "invalid terminal input namespace")
                if client_id not in record.sequences:
                    if len(record.sequences) >= INPUT_CLIENT_LIMIT:
                        raise HTTPException(
                            429, "Reconnect capacity reached; create a new terminal"
                        )
                    record.sequences[client_id] = InputSequence()
                # Claim before transport write: an uncertain write is never replayed.
                if record.sequences[client_id].accept(sequence):
                    await record.stream.write_in(data.encode())
                record.last_input = time.monotonic()
                return {"type": "ack", "seq": sequence}
            if kind == "resize":
                cols, rows = message.get("cols"), message.get("rows")
                if (
                    type(cols) is not int
                    or type(rows) is not int
                    or not 20 <= cols <= 500
                    or not 5 <= rows <= 200
                ):
                    raise HTTPException(422, "invalid terminal dimensions")
                await record.process.resize(h=rows, w=cols)
            elif kind == "interrupt":
                await record.stream.write_in(b"\x03")
            else:
                raise HTTPException(422, "unknown session input")
        return record.snapshot()

    async def stop(self, session_id: str) -> dict[str, Any]:
        record = self.get(session_id)
        if record.status not in {"starting", "running", "stopping"}:
            return record.snapshot()
        record.status = "stopping"
        await record.container.delete(force=True, v=True)
        if record.reader is not None:
            record.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await record.reader
        record.append("", final=True)
        await self._measure_terminal(record)
        record.status = "stopped"
        record.finished_at = time.monotonic()
        if self.jobs._workspace_holders.get(record.request.workspace_key) == session_id:
            self.jobs._workspace_holders.pop(record.request.workspace_key, None)
        return record.snapshot()

    async def close(self) -> None:
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task
        for session_id in list(self.records):
            with contextlib.suppress(Exception):
                await self.stop(session_id)


def install_session_routes(
    app: Any, jobs: JobManager, settings: Settings, require_token: Any
) -> SessionManager:
    manager = SessionManager(jobs, settings)
    router = APIRouter(prefix="/v1/sessions", dependencies=[Depends(require_token)])

    @router.post("")
    async def create_session(request: SessionRequest) -> dict[str, Any]:
        return (await manager.create(request)).snapshot()

    @router.get("/{session_id}")
    async def get_session(session_id: str, after: int | None = None) -> dict[str, Any]:
        return manager.get(session_id).snapshot(after)

    @router.post("/{session_id}/input")
    async def session_input(session_id: str, message: dict[str, Any]) -> dict[str, Any]:
        return await manager.input(session_id, message)

    @router.post("/{session_id}/stop")
    async def stop_session(session_id: str) -> dict[str, Any]:
        return await manager.stop(session_id)

    app.include_router(router)
    return manager
