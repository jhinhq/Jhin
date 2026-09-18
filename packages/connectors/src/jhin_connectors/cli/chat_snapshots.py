"""Immutable chat checkpoints and isolated colleague working copies."""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_connectors.cli.runner_client import workspace_file_operation
from jhin_db.models import (
    AuditEvent,
    ChatProject,
    Conversation,
    FileCheckpoint,
    FileRevision,
    SandboxWorkspace,
    Task,
)
from jhin_media.files import FileStore, InvalidFile, validate_relative_path
from jhin_media.managed_files import publish_file
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.errors import ToolExecutionError

if TYPE_CHECKING:
    from jhin_connectors.cli.workspace import WorkspaceBinding


async def prepare_chat_disk(ctx: ToolExecutionContext, binding: WorkspaceBinding) -> None:
    if ctx.session_factory is None or binding.kind not in {"conversation", "delegated"}:
        return
    async with ctx.session_factory() as db:
        task = await db.get(Task, ctx.task_id)
        if task is None or task.conversation_id is None:
            return
        chat = await db.scalar(
            select(Conversation).where(Conversation.id == task.conversation_id).with_for_update()
        )
        if chat is None:
            return
        held = await db.scalar(
            select(SandboxWorkspace).where(SandboxWorkspace.id == binding.row_id).with_for_update()
        )
        if held is None or held.holder_run_id != ctx.run_id:
            raise ToolExecutionError(
                "Workspace ownership changed before initialization",
                code="workspace_lease_lost",
                side_effect_possible=False,
            )
        receipt = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == ctx.workspace_id,
                AuditEvent.target_id == ctx.run_id,
                AuditEvent.action == "chat.workspace.prepared",
            )
        )
        if receipt is not None:
            return
        if binding.kind == "delegated":
            source = await db.scalar(
                select(FileCheckpoint)
                .where(
                    FileCheckpoint.workspace_id == ctx.workspace_id,
                    FileCheckpoint.conversation_id == task.conversation_id,
                )
                .order_by(FileCheckpoint.created_at.desc())
                .limit(1)
            )
            if source:
                for path, revision_id in source.manifest_json.items():
                    revision = await db.scalar(
                        select(FileRevision).where(
                            FileRevision.id == UUID(revision_id),
                            FileRevision.workspace_id == ctx.workspace_id,
                        )
                    )
                    if revision is None:
                        raise InvalidFile("Colleague source checkpoint is unavailable")
                    content = await asyncio.to_thread(
                        FileStore().read, ctx.workspace_id, revision.sha256
                    )
                    await workspace_file_operation(
                        binding.key,
                        "stage",
                        {"path": path, "content_base64": base64.b64encode(content).decode()},
                    )
            source_id = str(source.id) if source else None
        else:
            await seed_project_source(db, chat, binding.key)
            checkpoint = await capture(
                db, ctx.workspace_id, task.conversation_id, ctx.run_id, binding.key, before=True
            )
            source_id = str(checkpoint.id) if checkpoint else None
        db.add(
            AuditEvent(
                workspace_id=ctx.workspace_id,
                actor_type="system",
                actor_id=ctx.agent_id,
                action="chat.workspace.prepared",
                target_type="agent_run",
                target_id=ctx.run_id,
                metadata_json={
                    "workspace_key": binding.key,
                    "source_checkpoint_id": source_id,
                    "isolated": binding.kind == "delegated",
                },
            )
        )
        await db.commit()


async def project_source(db: AsyncSession, chat: Conversation) -> FileCheckpoint | None:
    if chat.project_id is None:
        return None
    project = await db.scalar(
        select(ChatProject).where(
            ChatProject.id == chat.project_id, ChatProject.workspace_id == chat.workspace_id
        )
    )
    if project is None or not (project.source_revision or "").startswith("checkpoint:"):
        return None
    try:
        identity = UUID((project.source_revision or "").removeprefix("checkpoint:"))
    except ValueError as exc:
        raise InvalidFile("Saved project source revision is invalid") from exc
    source = await db.scalar(
        select(FileCheckpoint).where(
            FileCheckpoint.id == identity, FileCheckpoint.workspace_id == chat.workspace_id
        )
    )
    if source is None:
        raise InvalidFile("Saved project source checkpoint is unavailable")
    return source


async def seed_project_source(db: AsyncSession, chat: Conversation, key: str) -> None:
    if chat.source_conversation_id is not None:
        return
    existing = await db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.workspace_id == chat.workspace_id,
            AuditEvent.target_id == chat.id,
            AuditEvent.action == "chat.project.seeded",
        )
    )
    if existing:
        return
    project = (
        await db.scalar(
            select(ChatProject).where(
                ChatProject.id == chat.project_id, ChatProject.workspace_id == chat.workspace_id
            )
        )
        if chat.project_id
        else None
    )
    retained = project.source_manifest_json if project else {}
    if (
        retained
        and project
        and project.source_revision == f"checkpoint:{retained.get('checkpoint_id')}"
    ):
        if retained.get("schema_version") != 1 or len(retained.get("files", [])) > 256:
            raise InvalidFile("Saved project source manifest is invalid")
        total = 0
        for entry in retained.get("files", []):
            validate_relative_path(entry["path"])
            content = await asyncio.to_thread(FileStore().read, chat.workspace_id, entry["sha256"])
            total += len(content)
            if len(content) != entry["size_bytes"] or total > 50 * 1024 * 1024:
                raise InvalidFile("Saved project source size is invalid")
            await workspace_file_operation(
                key,
                "stage",
                {"path": entry["path"], "content_base64": base64.b64encode(content).decode()},
            )
        checkpoint_id = retained["checkpoint_id"]
    else:
        checkpoint_id = await _seed_legacy_project_source(db, chat, key)
        if checkpoint_id is None:
            return
    db.add(
        AuditEvent(
            workspace_id=chat.workspace_id,
            actor_type="system",
            action="chat.project.seeded",
            target_type="conversation",
            target_id=chat.id,
            metadata_json={
                "project_id": str(chat.project_id),
                "checkpoint_id": checkpoint_id,
                "workspace_key": key,
            },
        )
    )


async def _seed_legacy_project_source(db: AsyncSession, chat: Conversation, key: str) -> str | None:
    source = await project_source(db, chat)
    if source is None:
        return None
    for path, rid in source.manifest_json.items():
        revision = await db.scalar(
            select(FileRevision).where(
                FileRevision.id == UUID(rid), FileRevision.workspace_id == chat.workspace_id
            )
        )
        if revision is None:
            raise InvalidFile("Saved project source version is unavailable")
        content = await asyncio.to_thread(FileStore().read, chat.workspace_id, revision.sha256)
        await workspace_file_operation(
            key, "stage", {"path": path, "content_base64": base64.b64encode(content).decode()}
        )
    return str(source.id)


async def capture(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    run_id: UUID,
    key: str,
    *,
    before: bool = False,
    delegated: bool = False,
) -> FileCheckpoint | None:
    snapshot = await workspace_file_operation(key, "snapshot", {})
    manifest, excluded = {}, list(snapshot.get("excluded", []))
    for entry in snapshot.get("files", []):
        path = entry["path"]
        if delegated:
            path = f"colleagues/{run_id}/{path}"
        try:
            file = await publish_file(
                db,
                workspace_id,
                conversation_id,
                path,
                base64.b64decode(entry["content_base64"], validate=True),
                kind="artifact" if delegated else "working",
                run_id=run_id,
            )
            manifest[path] = str(file.current_revision_id)
        except (InvalidFile, ValueError) as exc:
            excluded.append({"path": path, "reason": str(exc)[:200]})
    if before:
        checkpoint = FileCheckpoint(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            source_run_id=run_id,
            label="Before agent changes",
            manifest_json=manifest,
            excluded_json=excluded,
        )
        db.add(checkpoint)
        await db.flush()
        return checkpoint
    db.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type="system",
            action="chat.workspace.captured",
            target_type="agent_run",
            target_id=run_id,
            metadata_json={
                "workspace_key": key,
                "manifest": manifest,
                "excluded": excluded,
                "isolated": delegated,
            },
        )
    )
    return None


async def capture_run_outputs(
    factory: async_sessionmaker[AsyncSession] | None, workspace_id: UUID, run_id: UUID
) -> None:
    if factory is None:
        return
    async with factory() as db:
        rows = list(
            await db.scalars(
                select(SandboxWorkspace).where(
                    SandboxWorkspace.workspace_id == workspace_id,
                    SandboxWorkspace.holder_run_id == run_id,
                    SandboxWorkspace.kind.in_(["conversation", "delegated"]),
                )
            )
        )
        for row in rows:
            if row.conversation_id is None:
                continue
            await db.scalar(
                select(Conversation).where(Conversation.id == row.conversation_id).with_for_update()
            )
            current = await db.scalar(
                select(SandboxWorkspace)
                .where(SandboxWorkspace.id == row.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is None or current.holder_run_id != run_id:
                continue
            try:
                async with db.begin_nested():
                    await capture(
                        db,
                        workspace_id,
                        row.conversation_id,
                        run_id,
                        row.workspace_key,
                        delegated=row.kind == "delegated",
                    )
            except Exception:
                # Snapshot failure never reruns a command, and never deletes
                # its disk. Expose an explicit retained-files recovery record.
                db.add(
                    AuditEvent(
                        workspace_id=workspace_id,
                        actor_type="system",
                        action="chat.workspace.capture_failed",
                        target_type="agent_run",
                        target_id=run_id,
                        metadata_json={"workspace_key": row.workspace_key, "files_retained": True},
                    )
                )
        await db.commit()
