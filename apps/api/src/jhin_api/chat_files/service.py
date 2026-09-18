"""File API application service; sandbox bytes cross the guarded runtime bridge."""

from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import hashlib
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.chat_files.schemas import FileOut
from jhin_api.deps import WorkspaceContext
from jhin_db.models import ChatProject, Conversation, FileCheckpoint, FileRevision, ManagedFile
from jhin_media.files import MAX_FILE_BYTES, FileStore, InvalidFile, validate_relative_path
from jhin_media.managed_files import FileAccessError, get_file, get_revision, publish_file


async def conversation(db: AsyncSession, workspace_id: UUID, conversation_id: UUID) -> Conversation:
    chat = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if chat is None:
        raise HTTPException(404, "Conversation not found")
    return chat


async def project(db: AsyncSession, workspace_id: UUID, project_id: UUID) -> ChatProject:
    row = await db.scalar(
        select(ChatProject).where(
            ChatProject.workspace_id == workspace_id, ChatProject.id == project_id
        )
    )
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


async def file_out(
    db: AsyncSession, file: ManagedFile, revision: FileRevision | None = None
) -> FileOut:
    revision = revision or await get_revision(db, file.workspace_id, file)
    prefix = f"/api/v1/workspaces/{file.workspace_id}/files/{file.id}"
    return FileOut(
        id=file.id,
        workspace_id=file.workspace_id,
        conversation_id=file.conversation_id,
        name=file.name,
        path=file.path,
        kind=file.kind,
        status=file.status,
        error=file.error,
        current_revision_id=revision.id,
        version=revision.version,
        mime_type=revision.mime_type,
        size_bytes=revision.size_bytes,
        sha256=revision.sha256,
        preview_kind=revision.preview_kind,
        extracted_text=revision.extracted_text[:8000],
        extraction_truncated=revision.extraction_truncated or len(revision.extracted_text) > 8000,
        created_at=file.created_at,
        updated_at=file.updated_at,
        download_url=f"{prefix}/download?revision_id={revision.id}",
        preview_url=f"{prefix}/preview?revision_id={revision.id}",
    )


async def workspace_operation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    operation: str,
    args: dict[str, Any],
    *,
    write: bool = False,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    from jhin_api.runtime.service import workspace_operation as runtime_operation

    return await runtime_operation(
        db,
        ctx,
        conversation_id,
        operation,
        args,
        write=write,
        expected_generation=expected_generation,
    )


def decoded_runtime_file(item: dict[str, Any]) -> tuple[str, bytes]:
    try:
        path = validate_relative_path(item["path"])
        encoded = item["content_base64"]
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES * 4 // 3 + 4):
            raise InvalidFile("Workspace file exceeds publication limit")
        content = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise HTTPException(502, "Runtime returned invalid file content") from exc
    if item.get("sha256") and hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise HTTPException(502, "Runtime file integrity check failed")
    return path, content


async def publish_workspace_file(
    db: AsyncSession, ctx: WorkspaceContext, conversation_id: UUID, path: str
) -> ManagedFile:
    await conversation(db, ctx.workspace_id, conversation_id)
    validate_relative_path(path)
    result = await workspace_operation(db, ctx, conversation_id, "read", {"path": path})
    _, data = decoded_runtime_file({**result, "path": path})
    return await publish_file(
        db, ctx.workspace_id, conversation_id, path, data, user_id=ctx.user.id
    )


async def save_content(
    db: AsyncSession,
    ctx: WorkspaceContext,
    file_id: UUID,
    content: str,
    expected_revision_id: UUID,
    lease_generation: int,
) -> ManagedFile:
    file = await get_file(db, ctx.workspace_id, file_id)
    # Lock the metadata before crossing runtime. The runtime independently CAS
    # checks current disk bytes and fencing generation, so stale editors cannot
    # overwrite agent/PTY edits that have not yet been published.
    await db.scalar(
        select(Conversation).where(Conversation.id == file.conversation_id).with_for_update()
    )
    await db.refresh(file)
    revision = await get_revision(db, ctx.workspace_id, file)
    if revision.id != expected_revision_id:
        raise FileAccessError("File changed since it was opened; reload before saving", 409)
    if revision.preview_kind not in {"text", "code"} and revision.mime_type not in {
        "text/csv",
        "text/tab-separated-values",
    }:
        raise HTTPException(422, "This format supports annotations, not direct text editing")
    if revision.size_bytes > 1_000_000:
        raise HTTPException(422, "Files larger than 1 MB must be edited through agent tools")
    data = content.encode("utf-8")
    expected_sha256: str | None = revision.sha256
    if file.kind == "upload":
        observed = await workspace_operation(
            db, ctx, file.conversation_id, "read", {"path": file.path, "allow_missing": True}
        )
        if observed.get("missing") is True:
            expected_sha256 = None
        elif observed.get("sha256") != revision.sha256:
            raise FileAccessError(
                "A different working file uses this upload's path; save it under another name", 409
            )
    await workspace_operation(
        db,
        ctx,
        file.conversation_id,
        "write",
        {
            "path": file.path,
            "content_base64": base64.b64encode(data).decode("ascii"),
            "expected_sha256": expected_sha256,
        },
        write=True,
        expected_generation=lease_generation,
    )
    return await publish_file(
        db,
        ctx.workspace_id,
        file.conversation_id,
        file.path,
        data,
        kind="working",
        user_id=ctx.user.id,
        expected_revision_id=expected_revision_id,
    )


async def create_checkpoint(
    db: AsyncSession, ctx: WorkspaceContext, conversation_id: UUID, label: str
) -> FileCheckpoint:
    await conversation(db, ctx.workspace_id, conversation_id)
    snapshot = await workspace_operation(db, ctx, conversation_id, "snapshot", {})
    manifest: dict[str, Any] = {}
    excluded = list(snapshot.get("excluded", []))
    for item in snapshot.get("files", [])[:500]:
        path, data = decoded_runtime_file(item)
        try:
            file = await publish_file(
                db,
                ctx.workspace_id,
                conversation_id,
                path,
                data,
                kind="working",
                user_id=ctx.user.id,
            )
            manifest[path] = str(file.current_revision_id)
        except InvalidFile as exc:
            excluded.append({"path": path, "reason": str(exc)})
    if len(snapshot.get("files", [])) > 500:
        excluded.append({"path": "*", "reason": "Checkpoint limit of 500 files reached"})
    checkpoint = FileCheckpoint(
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        label=label,
        manifest_json=manifest,
        excluded_json=excluded,
        created_by_user_id=ctx.user.id,
    )
    db.add(checkpoint)
    await db.flush()
    return checkpoint


async def get_checkpoint(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID, checkpoint_id: UUID
) -> FileCheckpoint:
    checkpoint = await db.scalar(
        select(FileCheckpoint).where(
            FileCheckpoint.id == checkpoint_id,
            FileCheckpoint.workspace_id == workspace_id,
            FileCheckpoint.conversation_id == conversation_id,
        )
    )
    if checkpoint is None:
        raise HTTPException(404, "Checkpoint not found")
    return checkpoint


async def changes(db: AsyncSession, ctx: WorkspaceContext, conversation_id: UUID) -> dict[str, Any]:
    await conversation(db, ctx.workspace_id, conversation_id)
    checkpoint = await db.scalar(
        select(FileCheckpoint)
        .where(
            FileCheckpoint.workspace_id == ctx.workspace_id,
            FileCheckpoint.conversation_id == conversation_id,
        )
        .order_by(FileCheckpoint.created_at.desc(), FileCheckpoint.id.desc())
        .limit(1)
    )
    snapshot = await workspace_operation(db, ctx, conversation_id, "snapshot", {})
    manifest = checkpoint.manifest_json if checkpoint else {}
    actual: dict[str, dict[str, Any]] = {
        validate_relative_path(item["path"]): item for item in snapshot.get("files", [])[:500]
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(set(manifest) | set(actual)):
        previous = (
            await db.scalar(
                select(FileRevision).where(
                    FileRevision.id == UUID(manifest[path]),
                    FileRevision.workspace_id == ctx.workspace_id,
                )
            )
            if path in manifest
            else None
        )
        item = actual.get(path)
        if previous and item and item.get("sha256") == previous.sha256:
            continue
        before = previous.extracted_text if previous else ""
        after = ""
        binary = False
        if item:
            _, data = decoded_runtime_file(item)
            try:
                after = data.decode("utf-8")[:200_000]
            except UnicodeDecodeError:
                binary = True
        diff = (
            "Binary file changed"
            if binary or (previous and previous.preview_kind not in {"text", "code", "spreadsheet"})
            else "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )[:200_000]
        )
        rows.append(
            {
                "path": path,
                "status": "deleted"
                if item is None
                else "added"
                if previous is None
                else "modified",
                "before_revision_id": str(previous.id) if previous else None,
                "after_revision_id": None,
                "diff": diff,
                "current_sha256": item.get("sha256") if item else None,
            }
        )
    return {
        "items": rows,
        "excluded": snapshot.get("excluded", []),
        "checkpoint_id": str(checkpoint.id) if checkpoint else None,
    }


async def restore(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    checkpoint_id: UUID,
    paths: list[str],
    expected_revisions: dict[str, str | None],
    lease_generation: int,
) -> list[str]:
    checkpoint = await get_checkpoint(db, ctx.workspace_id, conversation_id, checkpoint_id)
    files: list[dict[str, Any]] = []
    publication: list[tuple[str, bytes]] = []
    for path in dict.fromkeys(paths):
        validate_relative_path(path)
        if path not in checkpoint.manifest_json or path not in expected_revisions:
            raise HTTPException(
                422, "Every restored path needs a checkpoint revision and expected current hash"
            )
        revision = await db.scalar(
            select(FileRevision).where(
                FileRevision.id == UUID(checkpoint.manifest_json[path]),
                FileRevision.workspace_id == ctx.workspace_id,
            )
        )
        if revision is None:
            raise HTTPException(409, "A checkpoint revision is unavailable")
        data = await asyncio.to_thread(FileStore().read, ctx.workspace_id, revision.sha256)
        files.append(
            {
                "path": path,
                "content_base64": base64.b64encode(data).decode("ascii"),
                "expected_sha256": expected_revisions[path],
            }
        )
        publication.append((path, data))
    await workspace_operation(
        db,
        ctx,
        conversation_id,
        "restore",
        {"files": files},
        write=True,
        expected_generation=lease_generation,
    )
    for path, data in publication:
        await publish_file(
            db, ctx.workspace_id, conversation_id, path, data, kind="working", user_id=ctx.user.id
        )
    return list(dict.fromkeys(paths))
