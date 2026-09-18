"""Scoped runtime gateway. Only this service and the tool worker reach the runner.

No browser request carries runner credentials or chooses a container, volume,
destination host, or port. PostgreSQL capability rows bind every operation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from opentelemetry.trace import Tracer
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.cli.conversation_workspace import (
    runtime_operation_needs_space,
    workspace_growth_error,
)
from jhin_connectors.cli.runner_client import runner_config
from jhin_db import create_engine, create_session_factory
from jhin_db.models import (
    AgentRun,
    Conversation,
    FileRevision,
    RuntimeSession,
    SandboxJob,
    SandboxWorkspace,
    Task,
    User,
    WorkspaceMembership,
)
from jhin_media.files import FileStore, validate_relative_path
from jhin_observability import (
    ObservabilitySettings,
    Observation,
    initialize_observability,
    noop_tracer,
    service_version,
)
from jhin_observability.workspace_metrics import workspace_metrics
from jhin_secrets.redaction import redact_event_dict

ACTIVE = {"starting", "running", "stopping"}
_NOOP_TRACER = noop_tracer()


def now() -> datetime:
    return datetime.now(UTC)


def alive(value: datetime | None) -> bool:
    return value is not None and value.replace(tzinfo=value.tzinfo or UTC) > now()


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def authority(
    db: AsyncSession, sid: UUID, token: str, *, kind: str, lock: bool = False
) -> RuntimeSession:
    query = select(RuntimeSession).where(RuntimeSession.id == sid, RuntimeSession.kind == kind)
    row = await db.scalar(query.with_for_update() if lock else query)
    if (
        row is None
        or not 32 <= len(token) <= 512
        or not secrets.compare_digest(row.ticket_hash, hashlib.sha256(token.encode()).hexdigest())
        or not alive(row.ticket_expires_at)
        or not alive(row.expires_at)
        or (kind == "ticket" and row.status not in {"starting", "running"})
    ):
        raise HTTPException(401, "Session ticket is invalid or expired")
    membership = await db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == row.user_id,
            WorkspaceMembership.workspace_id == row.workspace_id,
        )
    )
    user = await db.get(User, row.user_id)
    if membership is None or user is None or user.status != "active":
        raise HTTPException(403, "Workspace access was revoked")
    if row.config_json.get("write") and membership.role not in {"owner", "admin"}:
        raise HTTPException(403, "Runtime control requires an owner or admin")
    return row


async def binding_for(
    db: AsyncSession, row: RuntimeSession, *, write: bool, lock: bool = False, grow: bool = True
) -> SandboxWorkspace:
    if lock:
        # Journal triggers take this row too: all runtime writers use the
        # same Conversation -> SandboxWorkspace lock order.
        chat = await db.scalar(
            select(Conversation)
            .where(
                Conversation.id == row.conversation_id,
                Conversation.workspace_id == row.workspace_id,
            )
            .with_for_update()
        )
        if chat is None:
            raise HTTPException(404, "Chat not found")
    query = select(SandboxWorkspace).where(
        SandboxWorkspace.workspace_id == row.workspace_id,
        SandboxWorkspace.conversation_id == row.conversation_id,
        SandboxWorkspace.kind == "conversation",
    )
    binding = await db.scalar(query.with_for_update() if lock else query)
    if (
        binding is None
        or binding.lease_generation != row.lease_generation
        or binding.workspace_key != row.workspace_key
    ):
        raise HTTPException(409, "Workspace ownership changed")
    if write and binding.holder_user_id != row.user_id:
        raise HTTPException(409, "Return to the chat and take control before writing")
    if write:
        run_ids = [value for value in (binding.holder_run_id, binding.last_holder_run_id) if value]
        if run_ids:
            pending = await db.scalar(
                select(SandboxJob.id)
                .where(
                    SandboxJob.workspace_id == row.workspace_id,
                    SandboxJob.run_id.in_(run_ids),
                    or_(
                        SandboxJob.status.in_(["pending", "running"]),
                        and_(
                            SandboxJob.status == "failed", SandboxJob.error_code == "runner_error"
                        ),
                    ),
                )
                .limit(1)
            )
            if pending is not None:
                raise HTTPException(409, "A previous command is still active or unconfirmed")
        if binding.holder_run_id:
            run = await db.get(AgentRun, binding.holder_run_id)
            if run and run.status not in {"completed", "failed", "cancelled"}:
                raise HTTPException(409, "The agent still owns this workspace")
        if grow and (error := await workspace_growth_error(db, binding)):
            raise HTTPException(507, error)
    return binding


async def runner_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    base, token = runner_config()
    async with httpx.AsyncClient(timeout=100) as client:
        response = await client.request(
            method, base + path, headers={"Authorization": f"Bearer {token}"}, json=payload
        )
    if response.is_error:
        try:
            detail = str(response.json().get("detail", "Runner refused the operation"))[:400]
        except ValueError:
            detail = "Runner refused the operation"
        raise HTTPException(response.status_code, detail)
    return cast(dict[str, Any], response.json())


async def parent_session(db: AsyncSession, row: RuntimeSession, sid: UUID) -> RuntimeSession:
    parent = await db.scalar(
        select(RuntimeSession).where(
            RuntimeSession.id == sid,
            RuntimeSession.workspace_id == row.workspace_id,
            RuntimeSession.conversation_id == row.conversation_id,
            RuntimeSession.kind.in_(["terminal", "preview"]),
        )
    )
    if parent is None:
        raise HTTPException(404, "Session not found")
    return parent


async def seed_preview(db: AsyncSession, parent: RuntimeSession) -> None:
    files: list[dict[str, Any]] = []
    total = 0
    store = FileStore()
    for path, rid in parent.config_json.get("manifest", {}).items():
        validate_relative_path(path)
        revision = await db.scalar(
            select(FileRevision).where(
                FileRevision.id == UUID(rid), FileRevision.workspace_id == parent.workspace_id
            )
        )
        if revision is None:
            raise HTTPException(409, "Published source version is unavailable")
        data = await asyncio.to_thread(store.read, parent.workspace_id, revision.sha256)
        total += len(data)
        if total > 32 * 1024 * 1024 or len(files) >= 256:
            raise HTTPException(413, "Preview source exceeds the 32 MiB / 256 file limit")
        files.append(
            {
                "path": path,
                "content_base64": base64.b64encode(data).decode(),
                "expected_sha256": None,
            }
        )
    if not files:
        raise HTTPException(422, "Preview has no published source files")
    await runner_request(
        "POST",
        f"/v1/workspaces/{parent.workspace_key}/operation",
        {"operation": "restore", "args": {"files": files}},
    )


async def execute_operation(
    db: AsyncSession, row: RuntimeSession, payload: dict[str, Any]
) -> dict[str, Any]:
    action = row.config_json["action"]
    # Hold this fence through the runner response: control transfer and agent
    # acquisition wait for a confirmed filesystem/process mutation.
    binding = await binding_for(
        db,
        row,
        write=bool(row.config_json.get("write")),
        lock=True,
        grow=runtime_operation_needs_space(action, payload),
    )
    if action == "seed_project":
        from jhin_connectors.cli.chat_snapshots import seed_project_source

        chat = await db.get(Conversation, row.conversation_id)
        if chat is None:
            raise HTTPException(404, "Chat not found")
        await seed_project_source(db, chat, binding.workspace_key)
        return {"seeded": True}
    if action == "files":
        result = await runner_request(
            "POST", f"/v1/workspaces/{binding.workspace_key}/operation", payload
        )
        record_runtime_size(binding, result)
        return result
    if action == "import":
        chat = await db.get(Conversation, row.conversation_id)
        if chat is None:
            raise HTTPException(404, "Chat not found")
        legacy = await db.scalar(
            select(SandboxWorkspace)
            .where(
                SandboxWorkspace.workspace_id == row.workspace_id,
                SandboxWorkspace.agent_id == chat.primary_agent_id,
                SandboxWorkspace.kind == "agent",
            )
            .with_for_update()
        )
        if legacy is None:
            raise HTTPException(404, "The agent has no previous workspace to import")
        if legacy.holder_run_id:
            run = await db.get(AgentRun, legacy.holder_run_id)
            if run and run.status not in {"completed", "failed", "cancelled"}:
                raise HTTPException(
                    409, "The previous workspace is in use; wait for that agent to finish"
                )
        snapshot = await runner_request(
            "POST",
            f"/v1/workspaces/{legacy.workspace_key}/operation",
            {"operation": "snapshot", "args": {}},
        )
        paths = payload.get("paths")
        if paths is not None:
            for path in paths:
                validate_relative_path(path)
        files = [
            {**item, "expected_sha256": None}
            for item in snapshot["files"]
            if paths is None or item["path"] in paths
        ]
        if paths and set(paths) != {item["path"] for item in files}:
            raise HTTPException(
                409,
                "Some requested files were excluded or no longer exist; "
                "original files are preserved",
            )
        if files:
            restored = await runner_request(
                "POST",
                f"/v1/workspaces/{binding.workspace_key}/operation",
                {"operation": "restore", "args": {"files": files}},
            )
            record_runtime_size(binding, restored)
        chat.workspace_version = 1
        return {
            "copied": [item["path"] for item in files],
            "excluded": snapshot.get("excluded", []),
        }
    if action == "seed_branch":
        source = await db.get(Conversation, UUID(payload["source_conversation_id"]))
        if source is None or source.workspace_id != row.workspace_id:
            raise HTTPException(404, "Source chat not found")
        # A text-only checkpoint is a valid branch. Its new workspace stays
        # empty; unlike a preview, it needs no published files to initialize.
        if not payload["manifest"]:
            return {"restored": []}
        temporary = RuntimeSession(
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            user_id=row.user_id,
            kind="preview",
            workspace_key=binding.workspace_key,
            config_json={"manifest": payload["manifest"]},
        )
        await seed_preview(db, temporary)
        return {"restored": list(payload["manifest"])}
    if action == "cancel":
        job = await db.scalar(
            select(SandboxJob).where(
                SandboxJob.id == UUID(payload["job_id"]),
                SandboxJob.workspace_id == row.workspace_id,
                SandboxJob.task_id == UUID(payload["task_id"]),
            )
        )
        task = await db.get(Task, UUID(payload["task_id"]))
        if (
            not job
            or not task
            or task.conversation_id != row.conversation_id
            or not task.metadata_json.get("stop_requested_at")
        ):
            raise HTTPException(409, "Exact invocation cancellation has not been requested")
        from jhin_tool_worker.sandbox_reconcile import _closure_for

        observed_status, observed_error = job.status, job.error_code
        state = await runner_request("POST", f"/v1/jobs/{job.id}/cancel")
        closure = _closure_for(state)
        if closure is not None:
            values, _action, _evidence = closure
            await db.execute(
                update(SandboxJob)
                .where(
                    SandboxJob.id == job.id,
                    SandboxJob.workspace_id == row.workspace_id,
                    SandboxJob.status == observed_status,
                    SandboxJob.error_code.is_not_distinct_from(observed_error),
                )
                .values(**values, completed_at=now())
                .execution_options(synchronize_session=False)
            )
        return state
    parent = await parent_session(db, row, UUID(payload["session_id"]))
    if action == "start":
        if not alive(parent.expires_at) or parent.expires_at is None:
            raise HTTPException(409, "Session expired before startup")
        if parent.status != "starting":
            raise HTTPException(409, "Session was already dispatched; inspect its status")
        if parent.kind == "preview":
            await seed_preview(db, parent)
        command = parent.config_json.get("command", "")
        if parent.kind == "preview":
            from jhin_connectors.cli.preview_adapters import preview_command

            command = preview_command(
                parent.config_json.get("framework", "static"),
                parent.config_json.get("port", 3000),
                parent.id,
                command,
            )
        state = await runner_request(
            "POST",
            "/v1/sessions",
            {
                "session_id": str(parent.id),
                "workspace_key": parent.workspace_key,
                "kind": parent.kind,
                "network": parent.network,
                "command": command,
                "port": parent.config_json.get("port", 3000),
                "expires_in_seconds": max(
                    60,
                    min(
                        28800,
                        int(
                            (
                                parent.expires_at.replace(tzinfo=parent.expires_at.tzinfo or UTC)
                                - now()
                            ).total_seconds()
                        ),
                    ),
                ),
            },
        )
    elif action == "stop":
        try:
            state = await runner_request("POST", f"/v1/sessions/{parent.id}/stop")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            state = {
                "status": "stopped",
                "error": "Runtime restarted; the process is no longer running",
            }
    elif action == "interrupt":
        state = await runner_request(
            "POST", f"/v1/sessions/{parent.id}/input", {"type": "interrupt"}
        )
    elif action == "status":
        try:
            state = await runner_request("GET", f"/v1/sessions/{parent.id}")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            state = {
                "status": "stopped",
                "error": "Runtime restarted; retained files remain available",
            }
    else:
        raise HTTPException(422, "Unsupported runtime operation")
    parent.status = state.get("status", parent.status)
    parent.state_json = {**parent.state_json, **state}
    if parent.kind == "terminal" and parent.lease_generation == binding.lease_generation:
        record_runtime_size(binding, state)
    return state


def record_runtime_size(binding: SandboxWorkspace, state: dict[str, Any]) -> None:
    size = state.get("workspace_size_bytes")
    try:
        measured_at = datetime.fromisoformat(state.get("workspace_size_measured_at", ""))
        if measured_at.tzinfo is None:
            return
    except (ValueError, TypeError):
        return
    if binding.size_measured_at and measured_at <= binding.size_measured_at.replace(
        tzinfo=binding.size_measured_at.tzinfo or UTC
    ):
        return
    if type(size) is int and size >= 0:
        binding.size_bytes = size
        binding.size_state = "unknown" if state.get("workspace_size_partial") else "measured"
        binding.size_measured_at = measured_at


async def record_terminal_size(
    db: AsyncSession, parent: RuntimeSession, state: dict[str, Any]
) -> None:
    if parent.kind != "terminal" or state.get("workspace_size_bytes") is None:
        return
    binding = await db.scalar(
        select(SandboxWorkspace)
        .where(
            SandboxWorkspace.workspace_id == parent.workspace_id,
            SandboxWorkspace.conversation_id == parent.conversation_id,
            SandboxWorkspace.workspace_key == parent.workspace_key,
        )
        .with_for_update()
    )
    if binding is not None and binding.lease_generation == parent.lease_generation:
        record_runtime_size(binding, state)


def retained_state(previous: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep one authoritative bounded tail; reject stale concurrent snapshots."""
    if int(snapshot.get("output_offset", 0)) < int(previous.get("output_offset", 0)):
        return previous
    return {**previous, **snapshot, "output": str(snapshot.get("output", ""))[-131072:]}


async def cleanup_sessions(factory: Any) -> None:
    metrics = workspace_metrics()
    async with factory() as db:
        stuck = await db.scalar(
            select(func.count())
            .select_from(RuntimeSession)
            .where(
                RuntimeSession.kind.in_(["terminal", "preview"]),
                RuntimeSession.status.in_(ACTIVE),
                or_(
                    RuntimeSession.expires_at <= now(),
                    and_(
                        RuntimeSession.status.in_(["starting", "stopping"]),
                        RuntimeSession.updated_at < now() - timedelta(minutes=2),
                    ),
                ),
            )
        )
        metrics.set_observable("runtime_stuck_sessions", [Observation(stuck or 0, {})])
        expired = list(
            await db.scalars(
                select(RuntimeSession.id)
                .where(
                    RuntimeSession.kind.in_(["terminal", "preview"]),
                    RuntimeSession.status.in_(ACTIVE),
                    RuntimeSession.expires_at <= now(),
                )
                .limit(64)
            )
        )
    for identity in expired:
        # One transaction per session: one unavailable runner response must
        # not expire ORM objects belonging to the next cleanup attempt.
        async with factory() as db:
            parent = await db.get(RuntimeSession, identity)
            if parent is None or parent.status not in ACTIVE:
                continue
            await db.scalar(
                select(Conversation)
                .where(
                    Conversation.id == parent.conversation_id,
                    Conversation.workspace_id == parent.workspace_id,
                )
                .with_for_update()
            )
            try:
                state = await runner_request("POST", f"/v1/sessions/{parent.id}/stop")
                parent.state_json = retained_state(parent.state_json, state)
                await record_terminal_size(db, parent, state)
            except HTTPException as exc:
                if exc.status_code != 404:
                    metrics.counter("runtime_session_cleanup_total").add(1, outcome="failed")
                    continue
            parent.status = "expired"
            await db.commit()
            metrics.counter("runtime_session_cleanup_total").add(1, outcome="completed")
    async with factory() as db:
        # Retained terminal/preview history is never pruned here. Capability
        # rows contain no user files and expire independently of the session.
        await db.execute(
            delete(RuntimeSession).where(
                RuntimeSession.kind.in_(["operation", "ticket"]),
                RuntimeSession.expires_at < now() - timedelta(days=7),
            )
        )
        await db.commit()


async def cleanup_loop(factory: Any) -> None:
    while True:
        await asyncio.sleep(30)
        with contextlib.suppress(Exception):
            await cleanup_sessions(factory)


def create_app(factory: Any = None, *, tracer: Tracer = _NOOP_TRACER) -> FastAPI:
    engine = None
    if factory is None:
        engine = create_engine(os.environ["DATABASE_URL"], tracer=tracer)
        factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cleanup = asyncio.create_task(cleanup_loop(factory))
        try:
            yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            if engine is not None:
                await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session_factory = factory

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/operations/{operation_id}")
    async def operation(operation_id: UUID, request: Request) -> dict[str, Any]:
        if int(request.headers.get("content-length", "0")) > 48 * 1024 * 1024:
            raise HTTPException(413, "Transfer exceeds the limit")
        body = await request.body()
        if len(body) > 48 * 1024 * 1024:
            raise HTTPException(413, "Transfer exceeds the limit")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, "Invalid operation payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(422, "Operation payload must be an object")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        async with factory() as db:
            row = await authority(db, operation_id, token, kind="operation", lock=True)
            if row.status != "starting" or not secrets.compare_digest(
                row.config_json.get("payload_hash", ""), digest(payload)
            ):
                raise HTTPException(
                    409, "Operation already consumed or arguments changed; inspect its result"
                )
            row.status = "running"
            await db.commit()  # Durable claim before any runner side effect.
            try:
                result = await execute_operation(db, row, payload)
                row.status = "completed"
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                row = await db.get(RuntimeSession, operation_id)
                row.status = "uncertain"
                row.error = (
                    "Operation failed or its response was lost; "
                    "inspect current state before repeating it"
                )
                await db.commit()
                raise

    async def ticket_session(
        db: AsyncSession,
        sid: UUID,
        token: str,
        *,
        origin: str | None = None,
        write: bool = False,
        grow: bool = True,
    ) -> tuple[RuntimeSession, RuntimeSession]:
        ticket_id, separator, plaintext = token.partition(".")
        if not separator:
            raise HTTPException(401, "Invalid session ticket")
        try:
            tid = UUID(ticket_id)
        except ValueError as exc:
            raise HTTPException(401, "Invalid session ticket") from exc
        ticket = await authority(db, tid, plaintext, kind="ticket")
        if ticket.config_json.get("session_id") != str(sid):
            raise HTTPException(403, "Ticket belongs to another session")
        parent = await parent_session(db, ticket, sid)
        if not alive(parent.expires_at) or parent.status not in ACTIVE:
            raise HTTPException(409, "Session ended")
        if parent.kind == "terminal":
            if ticket.config_json.get("origin") and origin != ticket.config_json["origin"]:
                raise HTTPException(403, "Terminal origin changed")
            if write and not ticket.config_json.get("write"):
                raise HTTPException(403, "This ticket permits viewing only")
            await binding_for(db, ticket, write=write, lock=write, grow=grow)
        return ticket, parent

    @app.websocket("/runtime/sessions/{session_id}/ws")
    async def terminal_socket(socket: WebSocket, session_id: UUID) -> None:
        protocols = socket.scope.get("subprotocols", [])
        if len(protocols) != 2 or protocols[0] != "jhin-session":
            await socket.close(code=1008)
            return
        token, origin = protocols[1], socket.headers.get("origin")
        try:
            async with factory() as db:
                _, parent = await ticket_session(db, session_id, token, origin=origin)
                if parent.kind != "terminal":
                    raise HTTPException(403, "Not a terminal")
            await socket.accept(subprotocol="jhin-session")
            offset = max(0, int(socket.query_params.get("after", "0")))
            lock = asyncio.Lock()

            async def send(payload: dict[str, Any]) -> None:
                async with lock:
                    await socket.send_json(payload)

            async def output() -> None:
                nonlocal offset
                while True:
                    async with factory() as db:
                        _, parent = await ticket_session(db, session_id, token, origin=origin)
                        state = await runner_request("GET", f"/v1/sessions/{session_id}")
                        start = int(state.get("output_start", 0))
                        chunk = str(state.get("output", ""))[max(0, offset - start) :]
                        if chunk:
                            await send(
                                {
                                    "type": "output",
                                    "data": chunk,
                                    "offset": state["output_offset"],
                                    "truncated": offset < start,
                                }
                            )
                        offset = state["output_offset"]
                        await send(
                            {
                                "type": "status",
                                "status": state["status"],
                                "exit_code": state.get("exit_code"),
                                "output_offset": offset,
                            }
                        )
                        # Retain bounded logs after runner restart, using a full
                        # sanitized snapshot rather than appending replays.
                        # Acquire the conversation lock before mutating a
                        # session, then reload after any concurrent stop.
                        await db.scalar(
                            select(Conversation)
                            .where(
                                Conversation.id == parent.conversation_id,
                                Conversation.workspace_id == parent.workspace_id,
                            )
                            .with_for_update()
                        )
                        await db.refresh(parent)
                        parent.state_json = retained_state(parent.state_json, state)
                        await record_terminal_size(db, parent, state)
                        if parent.status in ACTIVE:
                            parent.status = state["status"]
                        await db.commit()
                    if state["status"] not in ACTIVE:
                        return
                    await asyncio.sleep(0.4)

            async def inputs() -> None:
                while True:
                    raw = await socket.receive_text()
                    if len(raw) > 20_000:
                        raise HTTPException(413, "Terminal input exceeds limit")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise HTTPException(422, "Terminal input must be an object")
                    try:
                        async with factory() as db:
                            ticket, _ = await ticket_session(
                                db,
                                session_id,
                                token,
                                origin=origin,
                                write=True,
                                grow=message.get("type") not in {"interrupt", "resize"},
                            )
                            # The caller cannot choose another connection's input
                            # namespace, including on a renewed reconnect ticket.
                            message["client_id"] = str(ticket.id)
                            response = await runner_request(
                                "POST", f"/v1/sessions/{session_id}/input", message
                            )
                            await db.commit()
                    except HTTPException as exc:
                        if exc.status_code != 507:
                            raise
                        # Reject this input without closing the view or losing
                        # the same connection's interrupt/resize authority.
                        await send(
                            {"type": "error", "code": "workspace_quota", "message": exc.detail}
                        )
                        continue
                    if response.get("type") == "ack":
                        await send(response)

            tasks = [asyncio.create_task(output()), asyncio.create_task(inputs())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                await socket.send_json(
                    {
                        "type": "error",
                        "message": (
                            "Session disconnected. Reconnect to verify status; "
                            "input is never replayed."
                        ),
                    }
                )
        finally:
            with contextlib.suppress(Exception):
                await socket.close()

    # Preview transport is installed separately; it shares the same DB ticket
    # checks and never accepts a destination URL from a request.
    from jhin_tool_worker.preview_gateway import install_preview_routes

    install_preview_routes(app, factory, ticket_session, runner_request)
    return app


def main() -> None:
    # Capability paths and query strings must never enter access logs.
    runtime = initialize_observability(
        ObservabilitySettings().observability_config(
            service_name="runtime-gateway",
            service_version=service_version("jhin-tool-worker"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    try:
        uvicorn.run(
            create_app(tracer=runtime.tracer),
            host="0.0.0.0",
            port=8086,
            access_log=False,
            ws_max_size=262144,
        )
    finally:
        runtime.shutdown()
