"""Chat-owned workspaces: durable identity and a fenced exclusive writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import AgentRun, Conversation, SandboxJob, SandboxWorkspace, Task
from jhin_tools.errors import ToolExecutionError

TERMINAL = ("completed", "failed", "cancelled")


def runtime_operation_needs_space(action: str, payload: dict[str, Any]) -> bool:
    """Classify growth conservatively; recovery controls must stay available.

    Only deletion/truncation to zero is provably non-growing without reading
    current bytes. Nonempty replacements still need admission even when the
    client claims they shrink a file; the runner owns the revision check.
    """
    if action in {"stop", "interrupt", "status"}:
        return False
    if action != "files":
        return True
    operation, args = payload.get("operation"), payload.get("args", {})
    if operation in {"read", "browse", "snapshot", "list"}:
        return False
    if not isinstance(args, dict):
        return True
    values = args.get("files") if operation == "restore" else [args]
    if operation in {"write", "restore"} and isinstance(values, list) and values:
        return not all(
            isinstance(value, dict) and value.get("content_base64") in (None, "")
            for value in values
        )
    return True


async def workspace_growth_error(db: AsyncSession, row: SandboxWorkspace) -> str | None:
    """Admission against recorded sizes, never an eviction or a hard disk cap.

    Match the agent binder's limits and conservative charge for partial walks.
    Use one aggregate scalar so organizations with many retained chats do not
    load every disk row into the gateway on each terminal input.
    """
    from jhin_connectors.cli.workspace import max_workspace_bytes, total_workspace_bytes

    cap = max_workspace_bytes()
    if row.size_state == "unknown":
        return (
            "Workspace disk usage could not be fully measured. Files are preserved; "
            "inspect or export them and resolve the disk measurement before adding more data."
        )
    if row.size_bytes > cap:
        return (
            "Workspace storage quota reached. Files are preserved; export and remove selected "
            "files, or ask your administrator to raise the sandbox workspace limit."
        )
    size = case((SandboxWorkspace.size_bytes > 0, SandboxWorkspace.size_bytes), else_=0)
    charge = case((and_(SandboxWorkspace.size_state == "unknown", size < cap), cap), else_=size)
    total = await db.scalar(
        select(func.coalesce(func.sum(charge), 0)).where(
            SandboxWorkspace.workspace_id == row.workspace_id
        )
    )
    if int(total or 0) > total_workspace_bytes():
        return (
            "The organization workspace storage quota is reached. Files are preserved; "
            "remove selected working files or ask your administrator to raise the sandbox "
            "workspace total limit."
        )
    return None


async def has_unconfirmed_jobs(db: AsyncSession, workspace_id: UUID, run_id: UUID) -> bool:
    """A final model run does not prove its remote command has stopped."""
    return (
        await db.scalar(
            select(SandboxJob.id)
            .where(
                SandboxJob.workspace_id == workspace_id,
                SandboxJob.run_id == run_id,
                or_(
                    SandboxJob.status.not_in(("completed", "failed", "timeout", "cancelled")),
                    and_(SandboxJob.status == "failed", SandboxJob.error_code == "runner_error"),
                ),
            )
            .limit(1)
        )
        is not None
    )


if TYPE_CHECKING:
    from jhin_connectors.cli.workspace import WorkspaceBinding


def conversation_workspace_key(workspace_id: UUID, conversation_id: UUID) -> str:
    return f"conversation-{workspace_id.hex}-{conversation_id.hex}"


async def ensure_conversation_workspace(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
) -> SandboxWorkspace:
    # Lock the durable parent to serialize row creation on Postgres.
    conversation = await db.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if conversation is None:
        raise ToolExecutionError(
            "chat workspace unavailable", code="workspace_missing", side_effect_possible=False
        )
    row = await db.scalar(
        select(SandboxWorkspace)
        .where(
            SandboxWorkspace.workspace_id == workspace_id,
            SandboxWorkspace.conversation_id == conversation_id,
            SandboxWorkspace.kind == "conversation",
        )
        .with_for_update()
    )
    if row is None:
        row = SandboxWorkspace(
            workspace_id=workspace_id,
            agent_id=None,
            conversation_id=conversation_id,
            kind="conversation",
            workspace_key=conversation_workspace_key(workspace_id, conversation_id),
            last_used_at=datetime.now(UTC),
        )
        db.add(row)
        await db.flush()
    return row


async def bind_conversation_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    agent_id: UUID,
    run_id: UUID,
    *,
    enforce_size: bool = True,
) -> WorkspaceBinding | None:
    from jhin_connectors.cli.workspace import (
        WorkspaceBinding,
        _charge,
        max_workspace_bytes,
        total_workspace_bytes,
    )

    async with factory() as db:
        conversation = await db.scalar(
            select(Conversation)
            .join(
                Task,
                Task.conversation_id == Conversation.id,
            )
            .join(AgentRun, AgentRun.task_id == Task.id)
            .where(
                AgentRun.id == run_id,
                AgentRun.agent_id == agent_id,
                AgentRun.workspace_id == workspace_id,
                Conversation.workspace_id == workspace_id,
            )
        )
        if conversation is None or getattr(conversation, "workspace_version", 0) < 1:
            return None
        if enforce_size:
            disks = (
                await db.execute(
                    select(SandboxWorkspace.size_bytes, SandboxWorkspace.size_state).where(
                        SandboxWorkspace.workspace_id == workspace_id
                    )
                )
            ).all()
            if sum(_charge(size, state) for size, state in disks) > total_workspace_bytes():
                raise ToolExecutionError(
                    "Workspace storage quota reached. Export or remove selected files; "
                    "chat disks were preserved.",
                    code="workspace_tenant_full",
                    side_effect_possible=False,
                )
        task = await db.scalar(
            select(Task).join(AgentRun, AgentRun.task_id == Task.id).where(AgentRun.id == run_id)
        )
        delegated = task is not None and (
            task.parent_task_id is not None or task.metadata_json.get("origin") == "work_request"
        )
        if delegated:
            await db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
            row = await db.scalar(
                select(SandboxWorkspace).where(
                    SandboxWorkspace.workspace_id == workspace_id,
                    SandboxWorkspace.run_id == run_id,
                    SandboxWorkspace.kind == "delegated",
                )
            )
            if row is None:
                row = SandboxWorkspace(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    conversation_id=conversation.id,
                    kind="delegated",
                    run_id=run_id,
                    workspace_key="delegated-" + run_id.hex,
                    holder_run_id=run_id,
                    lease_generation=1,
                    last_holder_run_id=run_id,
                    last_used_at=datetime.now(UTC),
                )
                db.add(row)
                await db.flush()
            if enforce_size and (
                row.size_state == "unknown" or row.size_bytes > max_workspace_bytes()
            ):
                raise ToolExecutionError(
                    "Colleague workspace quota needs attention; files retained",
                    code="workspace_full",
                    side_effect_possible=False,
                )
            row.holder_run_id = run_id
            row.lease_expires_at = datetime.now(UTC) + timedelta(hours=24)
            result = WorkspaceBinding(
                key=row.workspace_key,
                kind="delegated",
                record_target_type="sandbox_workspace",
                record_target_id=row.id,
                durable=True,
                row_id=row.id,
            )
            await db.commit()
            return result
        row = await ensure_conversation_workspace(db, workspace_id, conversation.id)
        if row.holder_user_id is not None:
            raise ToolExecutionError(
                "A person currently controls this chat workspace. "
                "Return control to the agent to continue.",
                code="workspace_human_control",
                side_effect_possible=False,
            )
        if row.holder_run_id not in {None, run_id}:
            holder = await db.get(AgentRun, row.holder_run_id)
            if (
                holder is None
                or holder.status not in TERMINAL
                or await has_unconfirmed_jobs(db, workspace_id, row.holder_run_id)
            ):
                raise ToolExecutionError(
                    "Another run holds this chat workspace; wait for its confirmed completion.",
                    code="workspace_busy",
                    side_effect_possible=False,
                )
        if enforce_size and (row.size_state == "unknown" or row.size_bytes > max_workspace_bytes()):
            raise ToolExecutionError(
                "Chat workspace quota needs attention; its files have been preserved.",
                code="workspace_full",
                side_effect_possible=False,
            )
        if row.holder_run_id != run_id:
            row.lease_generation += 1
        row.holder_run_id = run_id
        row.last_holder_run_id = run_id
        row.last_used_at = datetime.now(UTC)
        row.lease_expires_at = datetime.now(UTC) + timedelta(hours=24)
        result = WorkspaceBinding(
            key=row.workspace_key,
            kind="conversation",
            record_target_type="sandbox_workspace",
            record_target_id=row.id,
            durable=True,
            row_id=row.id,
        )
        await db.commit()
        return result
