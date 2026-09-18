"""DB-authorized runtime requests. The API never receives runner credentials."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_connectors.cli.conversation_workspace import (
    ensure_conversation_workspace,
    has_unconfirmed_jobs,
    runtime_operation_needs_space,
    workspace_growth_error,
)
from jhin_db.models import (
    AgentRun,
    AuditEvent,
    ChatProject,
    Conversation,
    RuntimeSession,
    SandboxJob,
    SandboxWorkspace,
    Task,
)

ACTIVE = {"starting", "running", "stopping"}


def enabled() -> None:
    if os.environ.get("JHIN_AGENTIC_WORKSPACE", "1").lower() in {"0", "false", "off"}:
        raise HTTPException(404, "Agentic workspace rollout is disabled")


async def conversation(db: AsyncSession, ctx: WorkspaceContext, cid: UUID) -> Conversation:
    enabled()
    row = await db.scalar(
        select(Conversation).where(
            Conversation.id == cid, Conversation.workspace_id == ctx.workspace_id
        )
    )
    if row is None:
        raise HTTPException(404, "Chat not found")
    return row


async def workspace(
    db: AsyncSession, ctx: WorkspaceContext, cid: UUID, *, allow_legacy: bool = False
) -> SandboxWorkspace:
    chat = await conversation(db, ctx, cid)
    if chat.workspace_version < 1 and not allow_legacy:
        raise HTTPException(
            409, "Import the previous workspace to give this chat its own persistent files"
        )
    binding = await ensure_conversation_workspace(db, ctx.workspace_id, cid)
    if chat.project_id and chat.source_conversation_id is None and binding.holder_run_id is None:
        project = await db.scalar(
            select(ChatProject).where(
                ChatProject.id == chat.project_id, ChatProject.workspace_id == ctx.workspace_id
            )
        )
        seeded = await db.scalar(
            select(AuditEvent.id).where(
                AuditEvent.workspace_id == ctx.workspace_id,
                AuditEvent.target_id == cid,
                AuditEvent.action == "chat.project.seeded",
            )
        )
        if project and (project.source_revision or "").startswith("checkpoint:") and not seeded:
            await gateway_call(db, ctx, cid, binding, "seed_project", {})
    return binding


async def check_idle(db: AsyncSession, row: SandboxWorkspace) -> None:
    if row.holder_run_id:
        run = await db.get(AgentRun, row.holder_run_id)
        if (
            run is None
            or run.status not in {"completed", "failed", "cancelled"}
            or await has_unconfirmed_jobs(db, row.workspace_id, row.holder_run_id)
        ):
            raise HTTPException(
                409,
                "The agent still owns this workspace. Stop its active turn "
                "and wait for completion before taking control.",
            )
        row.holder_run_id = None


def check_human(
    ctx: WorkspaceContext, row: SandboxWorkspace, generation: int | None = None
) -> None:
    if str(ctx.role) not in {"owner", "admin"} or row.holder_user_id != ctx.user.id:
        raise HTTPException(
            403, "An owner or admin must take control before writing to this workspace"
        )
    if generation is not None and generation != row.lease_generation:
        raise HTTPException(
            409, "Workspace ownership changed. Reopen the current revision before saving."
        )


def issue_ticket(row: RuntimeSession, *, seconds: int = 300) -> str:
    ticket = secrets.token_urlsafe(32)
    row.ticket_hash = hashlib.sha256(ticket.encode()).hexdigest()
    row.ticket_expires_at = datetime.now(UTC) + timedelta(seconds=seconds)
    return ticket


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def gateway_call(
    db: AsyncSession,
    ctx: WorkspaceContext,
    cid: UUID,
    binding: SandboxWorkspace,
    action: str,
    payload: dict[str, Any],
    *,
    write: bool = False,
) -> dict[str, Any]:
    if (
        write
        and runtime_operation_needs_space(action, payload)
        and (error := await workspace_growth_error(db, binding))
    ):
        raise HTTPException(507, error)
    # A one-shot capability binds exact arguments, actor, disk and generation.
    authority = RuntimeSession(
        workspace_id=ctx.workspace_id,
        conversation_id=cid,
        user_id=ctx.user.id,
        kind="operation",
        workspace_key=binding.workspace_key,
        lease_generation=binding.lease_generation,
        config_json={"action": action, "payload_hash": payload_hash(payload), "write": write},
        expires_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    db.add(authority)
    ticket = issue_ticket(authority, seconds=120)
    await db.commit()
    url = os.environ.get("JHIN_RUNTIME_GATEWAY_URL", "http://runtime-gateway:8086").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=105) as client:
            response = await client.post(
                f"{url}/internal/operations/{authority.id}",
                headers={"Authorization": f"Bearer {ticket}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            503,
            "Runtime connection lost. Inspect the current session or file before retrying; "
            "an action may have completed.",
        ) from exc
    if response.is_error:
        detail = "Runtime operation failed"
        with suppress(ValueError):
            detail = str(response.json().get("detail", detail))[:400]
        raise HTTPException(response.status_code, detail)
    result = response.json()
    if not isinstance(result, dict):
        raise HTTPException(502, "Runtime returned an invalid response")
    return result


async def workspace_operation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    operation: str,
    args: dict[str, Any],
    write: bool = False,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    binding = await workspace(db, ctx, conversation_id)
    await check_idle(db, binding)
    if write:
        check_human(ctx, binding, expected_generation)
    return await gateway_call(
        db,
        ctx,
        conversation_id,
        binding,
        "files",
        {"operation": operation, "args": args},
        write=write,
    )


def session_out(row: RuntimeSession) -> dict[str, Any]:
    return {
        **row.state_json,
        "id": str(row.id),
        "user_id": str(row.user_id),
        "kind": row.kind,
        "status": row.status,
        "cwd": "/workspace",
        "network": row.network,
        "lease_generation": row.lease_generation,
        "created_at": row.created_at.isoformat(),
        "revision_id": row.config_json.get("revision_id"),
        "error": row.error,
    }


async def get_session(
    db: AsyncSession, ctx: WorkspaceContext, cid: UUID, sid: UUID, kind: str | None = None
) -> RuntimeSession:
    await conversation(db, ctx, cid)
    row = await db.scalar(
        select(RuntimeSession).where(
            RuntimeSession.id == sid,
            RuntimeSession.workspace_id == ctx.workspace_id,
            RuntimeSession.conversation_id == cid,
        )
    )
    if row is None or row.kind not in ({kind} if kind else {"terminal", "preview"}):
        raise HTTPException(404, "Session not found")
    return row


async def runtime(db: AsyncSession, ctx: WorkspaceContext, cid: UUID) -> dict[str, Any]:
    chat = await conversation(db, ctx, cid)
    binding = await workspace(db, ctx, cid, allow_legacy=True)
    rows = list(
        await db.scalars(
            select(RuntimeSession)
            .where(
                RuntimeSession.conversation_id == cid,
                RuntimeSession.workspace_id == ctx.workspace_id,
                RuntimeSession.kind.in_(["terminal", "preview"]),
            )
            .order_by(RuntimeSession.created_at.desc())
            .limit(30)
        )
    )
    legacy = (
        await db.scalar(
            select(SandboxWorkspace).where(
                SandboxWorkspace.workspace_id == ctx.workspace_id,
                SandboxWorkspace.agent_id == chat.primary_agent_id,
                SandboxWorkspace.kind == "agent",
            )
        )
        if chat.workspace_version < 1
        else None
    )
    await db.commit()
    return {
        "workspace_key": binding.workspace_key,
        "cwd": "/workspace",
        "owner": "user" if binding.holder_user_id else "agent" if binding.holder_run_id else None,
        "owner_user_id": str(binding.holder_user_id) if binding.holder_user_id else None,
        "lease_generation": binding.lease_generation,
        "terminal": next((session_out(r) for r in rows if r.kind == "terminal"), None),
        "previews": [session_out(r) for r in rows if r.kind == "preview"],
        "legacy": chat.workspace_version < 1,
        "legacy_workspace_key": legacy.workspace_key if legacy else None,
    }


async def control(
    db: AsyncSession, ctx: WorkspaceContext, cid: UUID, action: str
) -> dict[str, Any]:
    binding = await workspace(db, ctx, cid)
    if action == "take":
        await check_idle(db, binding)
        if binding.holder_user_id not in {None, ctx.user.id}:
            raise HTTPException(409, "Another person controls this workspace")
        if binding.holder_user_id != ctx.user.id:
            binding.lease_generation += 1
        binding.holder_user_id = ctx.user.id
    else:
        check_human(ctx, binding)
        active = list(
            await db.scalars(
                select(RuntimeSession).where(
                    RuntimeSession.conversation_id == cid,
                    RuntimeSession.kind == "terminal",
                    RuntimeSession.status.in_(ACTIVE),
                )
            )
        )
        for session in active:
            await gateway_call(
                db, ctx, cid, binding, "stop", {"session_id": str(session.id)}, write=True
            )
        # Keep generation fenced through runner's confirmed stop.
        await db.refresh(binding)
        check_human(ctx, binding)
        binding.holder_user_id = None
        binding.lease_generation += 1
    await db.commit()
    return await runtime(db, ctx, cid)


async def cancel_task_invocations(
    db: AsyncSession, ctx: WorkspaceContext, task_id: UUID
) -> list[dict[str, Any]]:
    task = await db.scalar(
        select(Task).where(Task.id == task_id, Task.workspace_id == ctx.workspace_id)
    )
    if task is None or task.conversation_id is None:
        return []
    binding = await workspace(db, ctx, task.conversation_id, allow_legacy=True)
    jobs = list(
        await db.scalars(
            select(SandboxJob).where(
                SandboxJob.task_id == task_id,
                SandboxJob.workspace_id == ctx.workspace_id,
                or_(
                    SandboxJob.status.in_(["pending", "running"]),
                    and_(SandboxJob.status == "failed", SandboxJob.error_code == "runner_error"),
                ),
            )
        )
    )
    results = []
    for job in jobs:
        results.append(
            await gateway_call(
                db,
                ctx,
                task.conversation_id,
                binding,
                "cancel",
                {"job_id": str(job.id), "task_id": str(task_id)},
            )
        )
    return results


async def seed_branch_workspace(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    manifest: dict[str, str],
    source_conversation_id: UUID | None = None,
) -> dict[str, Any]:
    """Copy an authorized immutable checkpoint into a newly created empty chat."""
    binding = await workspace(db, ctx, conversation_id)
    await check_idle(db, binding)
    if source_conversation_id is None:
        chat = await conversation(db, ctx, conversation_id)
        source_conversation_id = chat.source_conversation_id
    if source_conversation_id is None:
        raise HTTPException(409, "Branch source must be recorded before its workspace is copied")
    return await gateway_call(
        db,
        ctx,
        conversation_id,
        binding,
        "seed_branch",
        {"manifest": manifest, "source_conversation_id": str(source_conversation_id)},
    )
