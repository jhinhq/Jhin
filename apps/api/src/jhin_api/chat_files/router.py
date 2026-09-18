"""Workspace-scoped managed file and project routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select

from jhin_api.chat_files import service
from jhin_api.chat_files.schemas import (
    AnnotationIn,
    AnnotationOut,
    CheckpointIn,
    CheckpointOut,
    ContentOut,
    ContentSave,
    FileListOut,
    FileOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    PublishIn,
    RestoreIn,
    RevisionListOut,
    RevisionOut,
)
from jhin_api.deps import AdminCtx, DbSession, MemberCtx, ViewerCtx
from jhin_api.security.csrf import csrf_protect
from jhin_db.models import ChatProject, FileAnnotation, FileCheckpoint, FileRevision, ManagedFile
from jhin_media.files import MAX_FILE_BYTES, FileStore, InvalidFile, StorageQuotaExceeded
from jhin_media.managed_files import FileAccessError, get_file, get_revision, publish_file


async def file_errors() -> AsyncIterator[None]:
    try:
        yield
    except FileAccessError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except StorageQuotaExceeded as exc:
        raise HTTPException(413, str(exc)) from exc
    except InvalidFile as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            503, "File storage is unavailable; restore the managed files volume"
        ) from exc


router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["chat files"],
    dependencies=[Depends(csrf_protect), Depends(file_errors)],
)


@router.get("/projects")
async def list_projects(ctx: ViewerCtx, db: DbSession) -> list[ProjectOut]:
    rows = await db.scalars(
        select(ChatProject)
        .where(ChatProject.workspace_id == ctx.workspace_id, ChatProject.archived.is_(False))
        .order_by(ChatProject.updated_at.desc())
        .limit(200)
    )
    return [ProjectOut.model_validate(row) for row in rows]


@router.post("/projects", status_code=201)
async def create_project(payload: ProjectCreate, ctx: MemberCtx, db: DbSession) -> ProjectOut:
    row = ChatProject(
        workspace_id=ctx.workspace_id, created_by_user_id=ctx.user.id, **payload.model_dump()
    )
    db.add(row)
    await db.commit()
    return ProjectOut.model_validate(row)


@router.get("/projects/{project_id}")
async def get_project(project_id: UUID, ctx: ViewerCtx, db: DbSession) -> ProjectOut:
    return ProjectOut.model_validate(await service.project(db, ctx.workspace_id, project_id))


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: UUID, payload: ProjectUpdate, ctx: MemberCtx, db: DbSession
) -> ProjectOut:
    row = await service.project(db, ctx.workspace_id, project_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is None and key not in {"repository_url", "source_revision"}:
            raise HTTPException(422, f"{key} may not be null")
        setattr(row, key, value)
    await db.commit()
    return ProjectOut.model_validate(row)


@router.delete("/projects/{project_id}", status_code=204)
async def archive_project(project_id: UUID, ctx: MemberCtx, db: DbSession) -> Response:
    row = await service.project(db, ctx.workspace_id, project_id)
    row.archived = True
    await db.commit()
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/files")
async def list_files(
    conversation_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    cursor: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> FileListOut:
    await service.conversation(db, ctx.workspace_id, conversation_id)
    query = select(ManagedFile).where(
        ManagedFile.workspace_id == ctx.workspace_id, ManagedFile.conversation_id == conversation_id
    )
    if cursor:
        query = query.where(ManagedFile.id < cursor)
    files = list(await db.scalars(query.order_by(ManagedFile.id.desc()).limit(limit + 1)))
    return FileListOut(
        items=[await service.file_out(db, row) for row in files[:limit]],
        has_more=len(files) > limit,
    )


@router.post("/conversations/{conversation_id}/files", status_code=201)
async def upload_file(
    conversation_id: UUID,
    ctx: MemberCtx,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    path: Annotated[str | None, Form()] = None,
) -> FileOut:
    await service.conversation(db, ctx.workspace_id, conversation_id)
    data = await file.read(MAX_FILE_BYTES + 1)
    await file.close()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds the 25 MiB limit")
    row = await publish_file(
        db,
        ctx.workspace_id,
        conversation_id,
        path or file.filename or "upload.txt",
        data,
        kind="upload",
        user_id=ctx.user.id,
    )
    await db.commit()
    return await service.file_out(db, row)


@router.post("/conversations/{conversation_id}/files/publish", status_code=201)
async def publish(
    conversation_id: UUID, payload: PublishIn, ctx: MemberCtx, db: DbSession
) -> FileOut:
    row = await service.publish_workspace_file(db, ctx, conversation_id, payload.path)
    if payload.title:
        row.name = payload.title
    await db.commit()
    return await service.file_out(db, row)


@router.get("/files/{file_id}")
async def file_metadata(file_id: UUID, ctx: ViewerCtx, db: DbSession) -> FileOut:
    return await service.file_out(db, await get_file(db, ctx.workspace_id, file_id))


@router.get("/files/{file_id}/versions")
async def versions(
    file_id: UUID,
    ctx: ViewerCtx,
    db: DbSession,
    before_version: int | None = Query(default=None, ge=1),
) -> RevisionListOut:
    await get_file(db, ctx.workspace_id, file_id)
    query = select(FileRevision).where(
        FileRevision.file_id == file_id, FileRevision.workspace_id == ctx.workspace_id
    )
    if before_version:
        query = query.where(FileRevision.version < before_version)
    rows = await db.scalars(query.order_by(FileRevision.version.desc()).limit(100))
    return RevisionListOut(items=[RevisionOut.model_validate(row) for row in rows])


@router.get("/files/{file_id}/content")
async def file_content(
    file_id: UUID, ctx: ViewerCtx, db: DbSession, revision_id: UUID | None = None
) -> ContentOut:
    file = await get_file(db, ctx.workspace_id, file_id)
    revision = await get_revision(db, ctx.workspace_id, file, revision_id)
    editable = (
        revision.preview_kind in {"text", "code"}
        or revision.mime_type in {"text/csv", "text/tab-separated-values"}
    ) and revision.size_bytes <= 1_000_000
    content = revision.extracted_text
    truncated = revision.extraction_truncated
    if editable:
        content = (
            await asyncio.to_thread(FileStore().read, ctx.workspace_id, revision.sha256)
        ).decode("utf-8-sig")
        truncated = False
    return ContentOut(
        file_id=file.id,
        revision_id=revision.id,
        path=file.path,
        content=content,
        truncated=truncated,
        editable=editable,
        sha256=revision.sha256,
    )


@router.put("/files/{file_id}/content")
async def save_file(file_id: UUID, payload: ContentSave, ctx: AdminCtx, db: DbSession) -> FileOut:
    file = await service.save_content(
        db, ctx, file_id, payload.content, payload.expected_revision_id, payload.lease_generation
    )
    await db.commit()
    return await service.file_out(db, file)


def _headers(filename: str, disposition: str = "attachment") -> dict[str, str]:
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN" if disposition == "inline" else "DENY",
        "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
        "Cache-Control": "private, no-store",
    }


@router.get("/files/{file_id}/download")
async def download(
    file_id: UUID, ctx: ViewerCtx, db: DbSession, revision_id: UUID | None = None
) -> Response:
    file = await get_file(db, ctx.workspace_id, file_id)
    revision = await get_revision(db, ctx.workspace_id, file, revision_id)
    data = await asyncio.to_thread(FileStore().read, ctx.workspace_id, revision.sha256)
    return Response(
        data, media_type=revision.mime_type, headers=_headers(PurePosixPath(file.path).name)
    )


@router.get("/files/{file_id}/preview")
async def preview(
    file_id: UUID, ctx: ViewerCtx, db: DbSession, revision_id: UUID | None = None
) -> Response:
    file = await get_file(db, ctx.workspace_id, file_id)
    revision = await get_revision(db, ctx.workspace_id, file, revision_id)
    if revision.preview_kind in {"image", "pdf"}:
        data = await asyncio.to_thread(FileStore().read, ctx.workspace_id, revision.sha256)
        return Response(data, media_type=revision.mime_type, headers=_headers(file.name, "inline"))
    return Response(
        revision.extracted_text, media_type="text/plain", headers=_headers(file.name, "inline")
    )


@router.get("/files/{file_id}/annotations")
async def list_annotations(
    file_id: UUID, ctx: ViewerCtx, db: DbSession
) -> dict[str, list[AnnotationOut]]:
    await get_file(db, ctx.workspace_id, file_id)
    rows = await db.scalars(
        select(FileAnnotation)
        .where(FileAnnotation.workspace_id == ctx.workspace_id, FileAnnotation.file_id == file_id)
        .order_by(FileAnnotation.created_at.desc())
        .limit(200)
    )
    return {"items": [AnnotationOut.model_validate(row) for row in rows]}


@router.post("/files/{file_id}/annotations", status_code=201)
async def annotate(
    file_id: UUID, payload: AnnotationIn, ctx: MemberCtx, db: DbSession
) -> AnnotationOut:
    file = await get_file(db, ctx.workspace_id, file_id)
    await get_revision(db, ctx.workspace_id, file, payload.revision_id)
    if len(json.dumps(payload.location)) > 4096:
        raise HTTPException(422, "Annotation location is too large")
    row = FileAnnotation(
        workspace_id=ctx.workspace_id,
        file_id=file_id,
        revision_id=payload.revision_id,
        text=payload.text,
        location_json=payload.location,
        created_by_user_id=ctx.user.id,
    )
    db.add(row)
    await db.commit()
    return AnnotationOut.model_validate(row)


@router.get("/conversations/{conversation_id}/changes")
async def changes(conversation_id: UUID, ctx: ViewerCtx, db: DbSession) -> dict[str, Any]:
    return await service.changes(db, ctx, conversation_id)


@router.get("/conversations/{conversation_id}/checkpoints")
async def checkpoints(
    conversation_id: UUID, ctx: ViewerCtx, db: DbSession
) -> dict[str, list[CheckpointOut]]:
    await service.conversation(db, ctx.workspace_id, conversation_id)
    rows = await db.scalars(
        select(FileCheckpoint)
        .where(
            FileCheckpoint.workspace_id == ctx.workspace_id,
            FileCheckpoint.conversation_id == conversation_id,
        )
        .order_by(FileCheckpoint.created_at.desc())
        .limit(100)
    )
    return {"items": [CheckpointOut.model_validate(row) for row in rows]}


@router.post("/conversations/{conversation_id}/checkpoints", status_code=201)
async def checkpoint(
    conversation_id: UUID, payload: CheckpointIn, ctx: MemberCtx, db: DbSession
) -> CheckpointOut:
    row = await service.create_checkpoint(db, ctx, conversation_id, payload.label)
    await db.commit()
    return CheckpointOut.model_validate(row)


@router.post("/conversations/{conversation_id}/checkpoints/{checkpoint_id}/restore")
async def restore(
    conversation_id: UUID, checkpoint_id: UUID, payload: RestoreIn, ctx: AdminCtx, db: DbSession
) -> dict[str, list[str]]:
    restored = await service.restore(
        db,
        ctx,
        conversation_id,
        checkpoint_id,
        payload.paths,
        payload.expected_revisions,
        payload.lease_generation,
    )
    await db.commit()
    return {"restored": restored}
