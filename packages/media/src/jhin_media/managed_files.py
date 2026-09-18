"""Shared publication and pinned-context boundary for API and tool workers."""

from __future__ import annotations

import asyncio
import base64
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Conversation, FileCheckpoint, FileRevision, ManagedFile
from jhin_media.files import FileStore, InvalidFile, bounded_inspect_file, validate_relative_path
from jhin_observability.workspace_metrics import workspace_metrics


class FileAccessError(ValueError):
    def __init__(self, message: str, status_code: int = 404):
        super().__init__(message)
        self.status_code = status_code


async def get_file(db: AsyncSession, workspace_id: UUID, file_id: UUID) -> ManagedFile:
    file = await db.scalar(
        select(ManagedFile).where(
            ManagedFile.id == file_id, ManagedFile.workspace_id == workspace_id
        )
    )
    if file is None:
        raise FileAccessError("File not found")
    return file


async def get_revision(
    db: AsyncSession, workspace_id: UUID, file: ManagedFile, revision_id: UUID | None = None
) -> FileRevision:
    revision = await db.scalar(
        select(FileRevision).where(
            FileRevision.id == (revision_id or file.current_revision_id),
            FileRevision.file_id == file.id,
            FileRevision.workspace_id == workspace_id,
        )
    )
    if revision is None:
        raise FileAccessError("File version not found")
    return revision


def staged_input_path(file: ManagedFile, revision: FileRevision) -> str:
    return f"inputs/{file.id}/{revision.id}/{PurePosixPath(file.path).name}"


async def publish_file(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    path: str,
    data: bytes,
    *,
    kind: str = "artifact",
    user_id: UUID | None = None,
    run_id: UUID | None = None,
    store: FileStore | None = None,
    expected_revision_id: UUID | None = None,
) -> ManagedFile:
    validate_relative_path(path)
    if kind not in {"artifact", "upload", "working"}:
        raise InvalidFile("Invalid file publication kind")
    if len(PurePosixPath(path).name) > 300:
        raise InvalidFile("Filename exceeds 300 characters")
    # Lock chat, not just a possibly missing path, to serialize first publication.
    chat = await db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.workspace_id == workspace_id)
        .with_for_update()
    )
    if chat is None:
        raise FileAccessError("Conversation not found")
    file = await db.scalar(
        select(ManagedFile)
        .where(
            ManagedFile.workspace_id == workspace_id,
            ManagedFile.conversation_id == conversation_id,
            ManagedFile.path == path,
        )
        .with_for_update()
    )
    if expected_revision_id is not None and (
        file is None or file.current_revision_id != expected_revision_id
    ):
        raise FileAccessError("File changed since it was opened; reload before saving", 409)
    try:
        info = await asyncio.to_thread(bounded_inspect_file, path, data)
        blob = store or FileStore()
        digest = await asyncio.to_thread(blob.put, workspace_id, data)
    except Exception:
        workspace_metrics().counter("artifact_publications_total").add(1, outcome="failed")
        raise
    workspace_metrics().counter("artifact_publications_total").add(1, outcome="ok")
    if file is None:
        file = ManagedFile(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            path=path,
            name=PurePosixPath(path).name,
            kind=kind,
            created_by_user_id=user_id,
            version=0,
        )
        db.add(file)
        await db.flush()
    if file.current_revision_id:
        current = await get_revision(db, workspace_id, file)
        if current.sha256 == digest:
            if kind == "artifact":
                file.kind = kind
            return file
    revision = FileRevision(
        workspace_id=workspace_id,
        file_id=file.id,
        version=file.version + 1,
        sha256=digest,
        size_bytes=len(data),
        mime_type=info.mime_type,
        preview_kind=info.preview_kind,
        extracted_text=info.extracted_text,
        extraction_truncated=info.truncated,
        metadata_json={"width": info.width, "height": info.height},
        created_by_user_id=user_id,
        source_run_id=run_id,
    )
    db.add(revision)
    await db.flush()
    file.current_revision_id = revision.id
    file.version = revision.version
    file.status = "ready"
    file.error = None
    if kind == "artifact":
        file.kind = kind
    await db.flush()
    return file


async def pin_attachments(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    attachment_ids: list[UUID],
    context_refs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    chat = await db.scalar(
        select(Conversation.id).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if chat is None:
        raise FileAccessError("Conversation not found")
    choices: list[tuple[UUID, UUID | None]] = [(UUID(str(value)), None) for value in attachment_ids]
    for ref in context_refs or []:
        if ref.get("type") in {"file", "artifact"}:
            try:
                choices.append(
                    (
                        UUID(str(ref.get("id") or ref.get("file_id"))),
                        UUID(str(ref["revision_id"])) if ref.get("revision_id") else None,
                    )
                )
            except (ValueError, TypeError) as exc:
                raise FileAccessError("Invalid file context reference", 422) from exc
    if len(choices) > 20:
        raise FileAccessError("A turn can include at most 20 files", 422)
    refs: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    total = 0
    for file_id, revision_id in choices:
        file = await get_file(db, workspace_id, file_id)
        if file.status != "ready":
            raise FileAccessError("File processing has not completed", 409)
        revision = await get_revision(db, workspace_id, file, revision_id)
        if revision.id in seen:
            continue
        seen.add(revision.id)
        total += revision.size_bytes
        if total > 50 * 1024 * 1024:
            raise FileAccessError("Attached files exceed the 50 MiB per-turn limit", 422)
        refs.append(
            {
                "type": "file",
                "id": str(file.id),
                "revision_id": str(revision.id),
                "name": file.name,
                "path": file.path,
                "mime_type": revision.mime_type,
                "size_bytes": revision.size_bytes,
                "sha256": revision.sha256,
                "mounted_path": staged_input_path(file, revision),
            }
        )
    return refs


async def attachment_content(
    db: AsyncSession,
    workspace_id: UUID,
    references: list[dict[str, Any]],
    *,
    store: FileStore | None = None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    remaining = 400_000
    image_bytes = 0
    if len(references) > 20:
        raise FileAccessError("Too many file references", 422)
    for ref in references:
        try:
            file_id, revision_id = UUID(str(ref["id"])), UUID(str(ref["revision_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise FileAccessError("Invalid pinned file reference", 422) from exc
        file = await get_file(db, workspace_id, file_id)
        revision = await get_revision(db, workspace_id, file, revision_id)
        if revision.preview_kind == "image":
            image_bytes += revision.size_bytes
            if image_bytes > 25 * 1024 * 1024:
                raise FileAccessError("Image inputs exceed the 25 MiB model-input limit", 422)
            data = await asyncio.to_thread(
                (store or FileStore()).read, workspace_id, revision.sha256
            )
            content.append(
                {
                    "type": "image",
                    "mime_type": revision.mime_type,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                    "file_id": str(file.id),
                    "revision_id": str(revision.id),
                }
            )
        else:
            text = revision.extracted_text[:remaining]
            remaining -= len(text)
            suffix = (
                "\n[Extracted content truncated; use file tools for bounded reads.]"
                if revision.extraction_truncated or len(text) < len(revision.extracted_text)
                else ""
            )
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"File: {file.name} (version {revision.version}, {revision.id})\n"
                        f"Sandbox input: {staged_input_path(file, revision)}\n{text}{suffix}"
                    ),
                    "file_id": str(file.id),
                    "revision_id": str(revision.id),
                }
            )
    return content


async def clone_checkpoint_files(
    db: AsyncSession,
    workspace_id: UUID,
    source_conversation_id: UUID,
    destination_conversation_id: UUID,
    checkpoint_id: UUID,
) -> FileCheckpoint:
    """Fork an immutable snapshot into a new chat, sharing immutable blob bytes.

    The runtime hydrates the returned destination manifest into a fresh disk.
    Existing destination files cause a conflict; this is not an overwrite API.
    """
    checkpoint = await db.scalar(
        select(FileCheckpoint).where(
            FileCheckpoint.workspace_id == workspace_id,
            FileCheckpoint.conversation_id == source_conversation_id,
            FileCheckpoint.id == checkpoint_id,
        )
    )
    destination = await db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.id == destination_conversation_id,
        )
        .with_for_update()
    )
    if checkpoint is None or destination is None:
        raise FileAccessError("Branch source or destination not found")
    if await db.scalar(
        select(ManagedFile.id)
        .where(
            ManagedFile.workspace_id == workspace_id,
            ManagedFile.conversation_id == destination_conversation_id,
        )
        .limit(1)
    ):
        raise FileAccessError("Branch destination already contains files", 409)
    manifest: dict[str, Any] = {}
    for path, identifier in checkpoint.manifest_json.items():
        source = await db.scalar(
            select(FileRevision)
            .join(ManagedFile, FileRevision.file_id == ManagedFile.id)
            .where(
                FileRevision.workspace_id == workspace_id,
                FileRevision.id == UUID(identifier),
                ManagedFile.conversation_id == source_conversation_id,
                ManagedFile.path == path,
            )
        )
        if source is None:
            raise FileAccessError("Checkpoint contains an unavailable revision", 409)
        file = ManagedFile(
            workspace_id=workspace_id,
            conversation_id=destination_conversation_id,
            path=path,
            name=PurePosixPath(path).name,
            kind="working",
            version=1,
        )
        db.add(file)
        await db.flush()
        revision = FileRevision(
            workspace_id=workspace_id,
            file_id=file.id,
            version=1,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            mime_type=source.mime_type,
            preview_kind=source.preview_kind,
            extracted_text=source.extracted_text,
            extraction_truncated=source.extraction_truncated,
            metadata_json=source.metadata_json,
            created_by_user_id=source.created_by_user_id,
            source_run_id=source.source_run_id,
        )
        db.add(revision)
        await db.flush()
        file.current_revision_id = revision.id
        manifest[path] = str(revision.id)
    cloned = FileCheckpoint(
        workspace_id=workspace_id,
        conversation_id=destination_conversation_id,
        label=f"Branch: {checkpoint.label}"[:200],
        manifest_json=manifest,
        excluded_json=list(checkpoint.excluded_json),
    )
    db.add(cloned)
    await db.flush()
    return cloned


async def clone_attachment_references(
    db: AsyncSession,
    workspace_id: UUID,
    destination_conversation_id: UUID,
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Give a branch its own pinned inputs without copying immutable blob bytes.

    Deterministic source-revision paths reuse mappings across copied messages.
    Each invocation uses the normal 20-file/50-MiB turn limits. Caller commits.
    """
    destination = await db.scalar(
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.id == destination_conversation_id,
        )
        .with_for_update()
    )
    if destination is None:
        raise FileAccessError("Branch destination not found")
    pinned = await pin_attachments(db, workspace_id, destination_conversation_id, [], references)
    output: list[dict[str, Any]] = []
    for ref in pinned:
        original = await get_file(db, workspace_id, UUID(ref["id"]))
        source = await get_revision(db, workspace_id, original, UUID(ref["revision_id"]))
        if original.conversation_id == destination_conversation_id:
            output.append(ref)
            continue
        path = f"branch-inputs/{original.id}/{source.id}/{PurePosixPath(original.path).name}"
        file = await db.scalar(
            select(ManagedFile).where(
                ManagedFile.workspace_id == workspace_id,
                ManagedFile.conversation_id == destination_conversation_id,
                ManagedFile.path == path,
            )
        )
        if file is None:
            file = ManagedFile(
                workspace_id=workspace_id,
                conversation_id=destination_conversation_id,
                path=path,
                name=original.name,
                kind="upload",
                version=1,
            )
            db.add(file)
            await db.flush()
            revision = FileRevision(
                workspace_id=workspace_id,
                file_id=file.id,
                version=1,
                sha256=source.sha256,
                size_bytes=source.size_bytes,
                mime_type=source.mime_type,
                preview_kind=source.preview_kind,
                extracted_text=source.extracted_text,
                extraction_truncated=source.extraction_truncated,
                metadata_json={
                    **source.metadata_json,
                    "branch_source_file_id": str(original.id),
                    "branch_source_revision_id": str(source.id),
                },
                created_by_user_id=source.created_by_user_id,
                source_run_id=source.source_run_id,
            )
            db.add(revision)
            await db.flush()
            file.current_revision_id = revision.id
            await db.flush()
        else:
            revision = await get_revision(db, workspace_id, file)
            if revision.sha256 != source.sha256 or revision.metadata_json.get(
                "branch_source_revision_id"
            ) != str(source.id):
                raise FileAccessError("Branch attachment destination has changed", 409)
        output.extend(
            await pin_attachments(
                db,
                workspace_id,
                destination_conversation_id,
                [],
                [{"type": "file", "id": str(file.id), "revision_id": str(revision.id)}],
            )
        )
    return output


async def retained_checkpoint_manifest(
    db: AsyncSession, workspace_id: UUID, checkpoint: FileCheckpoint
) -> dict[str, Any]:
    """Capture project-owned blob references before the source chat can be removed."""
    if checkpoint.workspace_id != workspace_id:
        raise FileAccessError("Checkpoint not found")
    if len(checkpoint.manifest_json) > 256:
        raise FileAccessError("Saved project source exceeds the 256-file limit", 422)
    files: list[dict[str, Any]] = []
    total = 0
    for path, identifier in checkpoint.manifest_json.items():
        validate_relative_path(path)
        revision = await db.scalar(
            select(FileRevision)
            .join(ManagedFile, ManagedFile.id == FileRevision.file_id)
            .where(
                FileRevision.id == UUID(identifier),
                FileRevision.workspace_id == workspace_id,
                ManagedFile.conversation_id == checkpoint.conversation_id,
                ManagedFile.path == path,
            )
        )
        if revision is None:
            raise FileAccessError("Project source contains an unavailable version", 409)
        total += revision.size_bytes
        if total > 50 * 1024 * 1024:
            raise FileAccessError("Saved project source exceeds the 50 MiB limit", 422)
        files.append(
            {
                "path": path,
                "sha256": revision.sha256,
                "size_bytes": revision.size_bytes,
                "revision_id": str(revision.id),
            }
        )
    return {
        "schema_version": 1,
        "checkpoint_id": str(checkpoint.id),
        "files": files,
        "excluded": list(checkpoint.excluded_json),
    }
