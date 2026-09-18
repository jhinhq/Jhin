"""Scoped browser controls and transports for conversation workspaces."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from jhin_api.chat_files.router import file_errors
from jhin_api.chat_files.service import create_checkpoint
from jhin_api.deps import AdminCtx, DbSession, MemberCtx, ViewerCtx
from jhin_api.runtime import service
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import RuntimeSession
from jhin_media.managed_files import get_file, get_revision, retained_checkpoint_manifest

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}",
    tags=["chat runtime"],
    dependencies=[Depends(csrf_protect), Depends(file_errors)],
)


class ControlIn(BaseModel):
    action: Literal["take", "return"]


project_source_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects",
    tags=["chat projects"],
    dependencies=[Depends(csrf_protect), Depends(file_errors)],
)


class ProjectSourceIn(BaseModel):
    conversation_id: UUID
    checkpoint_id: UUID | None = None


@project_source_router.post("/{project_id}/source")
async def save_project_source(
    project_id: UUID, payload: ProjectSourceIn, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    from jhin_api.chat_files.service import get_checkpoint, project

    row = await project(db, ctx.workspace_id, project_id)
    source = (
        await get_checkpoint(db, ctx.workspace_id, payload.conversation_id, payload.checkpoint_id)
        if payload.checkpoint_id
        else await create_checkpoint(db, ctx, payload.conversation_id, "Saved project source")
    )
    row.source_manifest_json = await retained_checkpoint_manifest(db, ctx.workspace_id, source)
    row.source_revision = f"checkpoint:{source.id}"
    await db.commit()
    return {
        "project_id": str(row.id),
        "source_revision": row.source_revision,
        "checkpoint_id": str(source.id),
        "excluded": source.excluded_json,
    }


class TerminalIn(BaseModel):
    network: Literal["none", "internet"] = "none"


class PreviewIn(BaseModel):
    file_id: UUID
    revision_id: UUID | None = None
    framework: Literal["static", "vite", "next", "http"] = "static"
    command: str | None = Field(default=None, max_length=8000)
    port: int = Field(default=3000, ge=1024, le=65535)


class ImportIn(BaseModel):
    paths: list[str] | None = Field(default=None, max_length=256)


@router.get("/runtime")
async def runtime(conversation_id: UUID, ctx: ViewerCtx, db: DbSession) -> dict[str, Any]:
    return await service.runtime(db, ctx, conversation_id)


@router.get("/runtime/files")
async def browse_files(
    conversation_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    path: str = Query(default="", max_length=1024),
) -> dict[str, Any]:
    return await service.workspace_operation(db, ctx, conversation_id, "browse", {"path": path})


@router.post("/runtime/control")
async def control(
    conversation_id: UUID, payload: ControlIn, ctx: AdminCtx, db: DbSession
) -> dict[str, Any]:
    return await service.control(db, ctx, conversation_id, payload.action)


@router.post("/runtime/import-legacy")
async def import_legacy(
    conversation_id: UUID, payload: ImportIn, ctx: AdminCtx, db: DbSession
) -> dict[str, Any]:
    chat = await service.conversation(db, ctx, conversation_id)
    if chat.workspace_version >= 1:
        raise HTTPException(409, "This chat already has its own workspace")
    binding = await service.workspace(db, ctx, conversation_id, allow_legacy=True)
    await service.check_idle(db, binding)
    if binding.holder_user_id not in {None, ctx.user.id}:
        raise HTTPException(409, "Another person controls the workspace")
    binding.holder_user_id = ctx.user.id
    binding.lease_generation += 1
    await service.gateway_call(
        db, ctx, conversation_id, binding, "import", {"paths": payload.paths}, write=True
    )
    await db.refresh(chat)
    chat.workspace_version = 1
    await db.commit()
    return await service.runtime(db, ctx, conversation_id)


@router.post("/terminals", status_code=201)
async def create_terminal(
    conversation_id: UUID, payload: TerminalIn, ctx: AdminCtx, db: DbSession
) -> dict[str, Any]:
    binding = await service.workspace(db, ctx, conversation_id)
    await service.check_idle(db, binding)
    service.check_human(ctx, binding)
    active = await db.scalar(
        select(RuntimeSession).where(
            RuntimeSession.conversation_id == conversation_id,
            RuntimeSession.kind == "terminal",
            RuntimeSession.status.in_(service.ACTIVE),
        )
    )
    if active:
        return service.session_out(active)
    row = RuntimeSession(
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        user_id=ctx.user.id,
        kind="terminal",
        workspace_key=binding.workspace_key,
        lease_generation=binding.lease_generation,
        network=payload.network,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    db.add(row)
    await db.flush()
    await service.gateway_call(
        db, ctx, conversation_id, binding, "start", {"session_id": str(row.id)}, write=True
    )
    await db.refresh(row)
    return service.session_out(row)


@router.get("/terminals/{session_id}")
async def terminal(
    conversation_id: UUID, session_id: UUID, ctx: ViewerCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "terminal")
    binding = await service.workspace(db, ctx, conversation_id)
    await service.gateway_call(
        db, ctx, conversation_id, binding, "status", {"session_id": str(row.id)}
    )
    await db.refresh(row)
    return service.session_out(row)


async def mint_ticket(
    db: Any, ctx: Any, cid: UUID, parent: RuntimeSession, request: Request
) -> str:
    binding = await service.workspace(db, ctx, cid)
    write = (
        parent.kind == "terminal"
        and binding.holder_user_id == ctx.user.id
        and str(ctx.role) in {"admin", "owner"}
    )
    lifetime = 1800 if parent.kind == "terminal" else 7200
    ticket = RuntimeSession(
        workspace_id=ctx.workspace_id,
        conversation_id=cid,
        user_id=ctx.user.id,
        kind="ticket",
        workspace_key=binding.workspace_key,
        lease_generation=binding.lease_generation,
        expires_at=datetime.now(UTC) + timedelta(seconds=lifetime),
        config_json={
            "session_id": str(parent.id),
            "write": write,
            "origin": request.headers.get("origin"),
        },
    )
    db.add(ticket)
    plaintext = service.issue_ticket(ticket, seconds=lifetime)
    await db.commit()
    return f"{ticket.id}.{plaintext}"


@router.post("/terminals/{session_id}/ticket")
async def terminal_ticket(
    conversation_id: UUID, session_id: UUID, request: Request, ctx: ViewerCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "terminal")
    return {
        "ticket": await mint_ticket(db, ctx, conversation_id, row, request),
        "websocket_url": f"/runtime/sessions/{row.id}/ws",
    }


@router.post("/terminals/{session_id}/close")
async def close_terminal(
    conversation_id: UUID, session_id: UUID, ctx: AdminCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "terminal")
    binding = await service.workspace(db, ctx, conversation_id)
    service.check_human(ctx, binding)
    await service.gateway_call(
        db, ctx, conversation_id, binding, "stop", {"session_id": str(row.id)}, write=True
    )
    await db.refresh(row)
    return service.session_out(row)


@router.post("/terminals/{session_id}/interrupt")
async def interrupt_terminal(
    conversation_id: UUID, session_id: UUID, ctx: AdminCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "terminal")
    binding = await service.workspace(db, ctx, conversation_id)
    service.check_human(ctx, binding)
    return await service.gateway_call(
        db, ctx, conversation_id, binding, "interrupt", {"session_id": str(row.id)}, write=True
    )


@router.get("/previews")
async def list_previews(
    conversation_id: UUID, ctx: ViewerCtx, db: DbSession
) -> list[dict[str, Any]]:
    await service.conversation(db, ctx, conversation_id)
    rows = await db.scalars(
        select(RuntimeSession)
        .where(
            RuntimeSession.conversation_id == conversation_id,
            RuntimeSession.workspace_id == ctx.workspace_id,
            RuntimeSession.kind == "preview",
        )
        .order_by(RuntimeSession.created_at.desc())
        .limit(30)
    )
    return [service.session_out(row) for row in rows]


@router.post("/previews", status_code=201)
async def create_preview(
    conversation_id: UUID, payload: PreviewIn, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    binding = await service.workspace(db, ctx, conversation_id)
    file = await get_file(db, ctx.workspace_id, payload.file_id)
    if file.conversation_id != conversation_id:
        raise HTTPException(404, "Preview file is not in this chat")
    revision = await get_revision(db, ctx.workspace_id, file, payload.revision_id)
    if payload.framework == "static":
        # HTML artifacts are snapshots; their script never executes in the API.
        manifest = {"index.html": str(revision.id)}
        revision_id = str(revision.id)
    else:
        checkpoint = await create_checkpoint(db, ctx, conversation_id, "Preview source")
        manifest = {**checkpoint.manifest_json, file.path: str(revision.id)}
        revision_id = str(checkpoint.id)
    row = RuntimeSession(
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        user_id=ctx.user.id,
        kind="preview",
        workspace_key="pending",
        lease_generation=binding.lease_generation,
        network="internet" if payload.framework in {"vite", "next"} else "none",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        config_json={
            "manifest": manifest,
            "revision_id": revision_id,
            "framework": payload.framework,
            "file_id": str(file.id),
            "file_revision_id": str(revision.id),
            "port": payload.port,
            "command": payload.command or "",
        },
    )
    db.add(row)
    await db.flush()
    row.workspace_key = "preview-" + row.id.hex
    await service.gateway_call(
        db, ctx, conversation_id, binding, "start", {"session_id": str(row.id)}
    )
    await db.refresh(row)
    return service.session_out(row)


@router.post("/previews/{session_id}/ticket")
async def preview_ticket(
    conversation_id: UUID, session_id: UUID, request: Request, ctx: ViewerCtx, db: DbSession
) -> dict[str, str]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "preview")
    token = await mint_ticket(db, ctx, conversation_id, row, request)
    return {"url": f"/runtime/previews/{row.id}/{token}/"}


@router.post("/previews/{session_id}/stop")
async def stop_preview(
    conversation_id: UUID, session_id: UUID, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "preview")
    binding = await service.workspace(db, ctx, conversation_id)
    await service.gateway_call(
        db, ctx, conversation_id, binding, "stop", {"session_id": str(row.id)}
    )
    await db.refresh(row)
    return service.session_out(row)


@router.post("/previews/{session_id}/restart", status_code=201)
async def restart_preview(
    conversation_id: UUID, session_id: UUID, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    row = await service.get_session(db, ctx, conversation_id, session_id, "preview")
    binding = await service.workspace(db, ctx, conversation_id)
    await service.gateway_call(
        db, ctx, conversation_id, binding, "stop", {"session_id": str(row.id)}
    )
    # New process identity, same published source. Refresh-from-changes uses create.
    replacement = RuntimeSession(
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        user_id=ctx.user.id,
        kind="preview",
        workspace_key="pending",
        lease_generation=binding.lease_generation,
        network=row.network,
        config_json=dict(row.config_json),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    db.add(replacement)
    await db.flush()
    replacement.workspace_key = "preview-" + replacement.id.hex
    await service.gateway_call(
        db, ctx, conversation_id, binding, "start", {"session_id": str(replacement.id)}
    )
    await db.refresh(replacement)
    return service.session_out(replacement)


@router.post("/previews/{session_id}/refresh", status_code=201)
async def refresh_preview(
    conversation_id: UUID, session_id: UUID, ctx: MemberCtx, db: DbSession
) -> dict[str, Any]:
    previous = await service.get_session(db, ctx, conversation_id, session_id, "preview")
    config = previous.config_json
    payload = PreviewIn(
        file_id=UUID(config["file_id"]),
        framework=config["framework"],
        command=config.get("command"),
        port=config["port"],
    )
    replacement = await create_preview(conversation_id, payload, ctx, db)
    binding = await service.workspace(db, ctx, conversation_id)
    await service.gateway_call(
        db, ctx, conversation_id, binding, "stop", {"session_id": str(previous.id)}
    )
    return replacement
