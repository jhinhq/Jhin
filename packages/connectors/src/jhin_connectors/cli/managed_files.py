"""Materialize pinned inputs and publish verified sandbox deliverables.

The sandbox receives selected bytes through contained runner operations,
never storage credentials or an unrestricted host mount.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from jhin_connectors.cli.runner_client import SandboxRunnerError, workspace_file_operation
from jhin_connectors.cli.schemas import _validate_workspace_path
from jhin_db.models import AuditEvent, Conversation, Message, Task
from jhin_media.files import MAX_FILE_BYTES, FileStore, InvalidFile
from jhin_media.managed_files import (
    FileAccessError,
    get_file,
    get_revision,
    publish_file,
    staged_input_path,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.errors import ToolExecutionError


class FilePublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str
    path: str = Field(min_length=1, max_length=1024)
    title: str | None = Field(default=None, max_length=300)

    @field_validator("path")
    @classmethod
    def path_is_contained(cls, value: str) -> str:
        return _validate_workspace_path(value)


class FilePublishOutput(BaseModel):
    file_id: str
    revision_id: str
    path: str
    name: str
    mime_type: str
    size_bytes: int
    sha256: str
    version: int
    preview_kind: str
    download_url: str
    preview_url: str


async def stage_chat_inputs(ctx: ToolExecutionContext, workspace_key: str) -> None:
    """Stage each revision once per run, preserving subsequent agent edits.

    Runner stage is create-if-absent or same-hash success. Its audit receipt
    commits independently so later tool failures cannot cause input replay.
    """
    if ctx.session_factory is None:
        return
    async with ctx.session_factory() as db:
        task = await db.scalar(
            select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
        )
        if task is None or task.conversation_id is None:
            return
        chat = await db.scalar(
            select(Conversation).where(
                Conversation.id == task.conversation_id,
                Conversation.workspace_id == ctx.workspace_id,
            )
        )
        if chat is None or chat.workspace_version < 1:
            return
        messages = list(
            await db.scalars(
                select(Message)
                .where(
                    Message.workspace_id == ctx.workspace_id,
                    Message.conversation_id == chat.id,
                    Message.task_id == task.id,
                    Message.sender_type == "user",
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(20)
            )
        )
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates = list(task.metadata_json.get("attachments", []))
        for message in reversed(messages):
            if message.message_type == "instruction" and message.content_json.get("delivery") in {
                "pending",
                "delivered",
            }:
                continue
            candidates.extend(message.content_json.get("attachments", []))
        for ref in candidates:
            revision_id = str(ref.get("revision_id", ""))
            if revision_id and revision_id not in seen:
                refs.append(ref)
                seen.add(revision_id)
        refs = refs[-20:]
        if not refs:
            return
        receipts = list(
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == ctx.workspace_id,
                    AuditEvent.action == "chat.input.staged",
                    AuditEvent.target_id == ctx.run_id,
                )
            )
        )
        staged = {
            str(row.metadata_json.get("revision_id"))
            for row in receipts
            if row.metadata_json.get("workspace_key") == workspace_key
        }
        try:
            for ref in refs:
                if str(ref["revision_id"]) in staged:
                    continue
                file = await get_file(db, ctx.workspace_id, UUID(str(ref["id"])))
                revision = await get_revision(
                    db, ctx.workspace_id, file, UUID(str(ref["revision_id"]))
                )
                path = staged_input_path(file, revision)
                data = await asyncio.to_thread(FileStore().read, ctx.workspace_id, revision.sha256)
                await workspace_file_operation(
                    workspace_key,
                    "stage",
                    {
                        "path": path,
                        "content_base64": base64.b64encode(data).decode("ascii"),
                        "sha256": revision.sha256,
                    },
                )
                db.add(
                    AuditEvent(
                        workspace_id=ctx.workspace_id,
                        actor_type="system",
                        actor_id=ctx.agent_id,
                        action="chat.input.staged",
                        target_type="agent_run",
                        target_id=ctx.run_id,
                        metadata_json={
                            "file_id": str(file.id),
                            "revision_id": str(revision.id),
                            "path": path,
                            "sha256": revision.sha256,
                            "workspace_key": workspace_key,
                        },
                    )
                )
                await db.commit()
        except (
            FileAccessError,
            InvalidFile,
            FileNotFoundError,
            ValueError,
            KeyError,
            SandboxRunnerError,
        ) as exc:
            raise ToolExecutionError(
                "The selected input file could not be staged safely",
                code="input_staging_failed",
                side_effect_possible=False,
                hint=(
                    "An input may be unavailable or its staged copy changed. "
                    "Preserve that copy and select the original file again."
                ),
            ) from exc


async def file_publish(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    from jhin_connectors.cli.tools import _binding, _load_cli_connection

    data = FilePublishInput.model_validate(payload.model_dump())
    await _load_cli_connection(ctx, data.connection_id)
    task = await ctx.session.scalar(
        select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
    )
    if task is None or task.conversation_id is None:
        raise ToolExecutionError(
            "File publication requires a conversation",
            code="conversation_required",
            side_effect_possible=False,
        )
    binding = await _binding(ctx)
    try:
        result = await workspace_file_operation(binding.key, "read", {"path": data.path})
        encoded = result.get("content_base64", "")
        if not isinstance(encoded, str) or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 4:
            raise InvalidFile("File exceeds publication limit")
        content = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(content).hexdigest() != result.get("sha256"):
            raise InvalidFile("Workspace file integrity check failed")
        file = await publish_file(
            ctx.session,
            ctx.workspace_id,
            task.conversation_id,
            f"colleagues/{ctx.run_id}/{data.path}" if binding.kind == "delegated" else data.path,
            content,
            run_id=ctx.run_id,
        )
        if data.title:
            file.name = data.title
        revision = await get_revision(ctx.session, ctx.workspace_id, file)
        prefix = f"/api/v1/workspaces/{ctx.workspace_id}/files/{file.id}"
        return FilePublishOutput(
            file_id=str(file.id),
            revision_id=str(revision.id),
            path=file.path,
            name=file.name,
            mime_type=revision.mime_type,
            size_bytes=revision.size_bytes,
            sha256=revision.sha256,
            version=revision.version,
            preview_kind=revision.preview_kind,
            download_url=f"{prefix}/download?revision_id={revision.id}",
            preview_url=f"{prefix}/preview?revision_id={revision.id}",
        )
    except (FileAccessError, InvalidFile, ValueError, SandboxRunnerError) as exc:
        raise ToolExecutionError(
            "File publication failed",
            code="file_publication_failed",
            side_effect_possible=False,
            hint=str(exc)[:250],
        ) from exc


FILE_PUBLISH_TOOL = ToolDefinition(
    name="cli.file.publish",
    description=(
        "Return a generated file to the person in this chat. Snapshots the actual sandbox file "
        "into durable, versioned storage with preview/download links. Supports text/code, images, "
        "PDF, DOCX, XLSX and PPTX. Use after creating or revising a deliverable; a path in prose "
        "alone is not a downloadable file. To revise an existing artifact, save to its original "
        "output path and publish that same path again; this creates the next immutable version "
        "of the same artifact. Pinned input revisions remain unchanged. A new output path "
        "creates a separate artifact."
    ),
    risk=RiskLevel.READ,
    input_model=FilePublishInput,
    output_model=FilePublishOutput,
    required_capability="cli.file.read",
    scope_keys=("connection_id", "path"),
    redispatch_is_safe=True,
)
