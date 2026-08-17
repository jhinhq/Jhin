"""Task, run, and message business logic (plan 6.12-6.14, 8.3, 19).

The API starts AgentTaskWorkflow through the Temporal client and signals it
for pause/resume/cancel/instruction. Postgres rows (task, agent_run,
message, run_event) are the source of truth the UI reads; the workflow's
activities on the agent worker write them.

Ordering rule for starts: the task row (and any conversational message) is
committed *before* the workflow starts, so activities always find it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowHandle
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AgentRun, Message, RunEvent, Task, ToolCall
from jhin_domain import (
    AgentStatus,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput

MAX_PAGE_SIZE = 200
ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)


def _task_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")


def workflow_id_for(task_id: UUID) -> str:
    return f"task-{task_id}"


async def _get_active_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID) -> Agent:
    agent = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.status != AgentStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent '{agent.name}' is {agent.status}; only active agents take tasks",
        )
    return agent


async def _start_workflow(
    db: AsyncSession, temporal: TemporalClient, task: Task, agent_id: UUID, instruction: str
) -> None:
    """Start AgentTaskWorkflow for a committed task row.

    On failure the task is marked failed (it was already committed) and the
    caller receives 503 — nothing is left silently queued.
    """
    workflow_id = workflow_id_for(task.id)
    try:
        await temporal.start_workflow(
            "AgentTaskWorkflow",
            AgentTaskInput(
                workspace_id=str(task.workspace_id),
                task_id=str(task.id),
                agent_id=str(agent_id),
                instruction=instruction,
            ),
            id=workflow_id,
            task_queue=AGENT_TASK_QUEUE,
        )
    except (RPCError, TemporalError, OSError) as exc:
        task.state = TaskState.FAILED.value
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the task workflow (Temporal unavailable)",
        ) from exc
    task.temporal_workflow_id = workflow_id
    await db.commit()


async def create_task(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Task:
    agent_id: UUID | None = values.pop("agent_id", None)
    if agent_id is not None:
        await _get_active_agent(db, ctx.workspace_id, agent_id)

    task = Task(
        workspace_id=ctx.workspace_id,
        title=values["title"],
        description=values.get("description", ""),
        priority=values["priority"].value,
        assigned_agent_id=agent_id,
        correlation_id=new_uuid7(),
    )
    db.add(task)
    await db.flush()
    audit.record(
        db,
        action="task.created",
        target_type="task",
        target_id=task.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"title": task.title, "agent_id": str(agent_id) if agent_id else None},
    )
    await db.commit()

    if agent_id is not None:
        await _start_workflow(db, temporal, task, agent_id, task.description)
    return task


async def assign_task(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    agent_id: UUID,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Task:
    values["agent_id"] = agent_id
    return await create_task(
        db, ctx, temporal, values=values, request_id=request_id, ip_hash=ip_hash
    )


async def message_agent(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    agent_id: UUID,
    *,
    text: str,
    request_id: UUID,
    ip_hash: str,
) -> Task:
    """Conversational entry point (plan 17.5): message → task + run."""
    agent = await _get_active_agent(db, ctx.workspace_id, agent_id)

    title = text.strip().splitlines()[0][:120] or f"Message to {agent.name}"
    task = Task(
        workspace_id=ctx.workspace_id,
        title=title,
        description=text,
        assigned_agent_id=agent_id,
        correlation_id=new_uuid7(),
        metadata_json={"origin": "message"},
    )
    db.add(task)
    await db.flush()
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=agent_id,
            content_json={"text": text},
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    audit.record(
        db,
        action="agent.messaged",
        target_type="agent",
        target_id=agent_id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"task_id": str(task.id)},
    )
    await db.commit()

    await _start_workflow(db, temporal, task, agent_id, text)
    return task


# --- Reads ---


async def list_tasks(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    state: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Task], int]:
    limit = min(limit, MAX_PAGE_SIZE)
    query = select(Task).where(Task.workspace_id == workspace_id)
    if state:
        query = query.where(Task.state == state)
    if agent_id is not None:
        query = query.where(Task.assigned_agent_id == agent_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.scalars(
        query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), int(total)


async def get_task(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> Task:
    task = await db.scalar(
        select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
    )
    if task is None:
        raise _task_not_found()
    return task


MAX_TREE_DEPTH = 20  # hard bound when walking lineage (delegation depth is ~5)


async def get_task_tree(
    db: AsyncSession, workspace_id: UUID, task_id: UUID
) -> tuple[Task, list[Task]]:
    """Resolve the delegation chain around a task (plan 6.12, 45).

    Walks up parent_task_id to the lineage root, then collects every
    descendant breadth-first. Returns (root, all_tasks_in_lineage).
    """
    task = await get_task(db, workspace_id, task_id)

    root = task
    for _ in range(MAX_TREE_DEPTH):
        if root.parent_task_id is None:
            break
        parent = await db.scalar(
            select(Task).where(Task.id == root.parent_task_id, Task.workspace_id == workspace_id)
        )
        if parent is None:
            break
        root = parent

    tasks: list[Task] = [root]
    frontier = [root.id]
    for _ in range(MAX_TREE_DEPTH):
        if not frontier:
            break
        children = list(
            await db.scalars(
                select(Task)
                .where(
                    Task.parent_task_id.in_(frontier),
                    Task.workspace_id == workspace_id,
                )
                .order_by(Task.created_at)
            )
        )
        if not children:
            break
        tasks.extend(children)
        frontier = [child.id for child in children]
    return root, tasks


async def latest_run_status_by_task(
    db: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
) -> dict[UUID, str]:
    """Latest run status per task, for tree/lineage displays."""
    if not task_ids:
        return {}
    rows = await db.execute(
        select(AgentRun.task_id, AgentRun.status)
        .where(AgentRun.workspace_id == workspace_id, AgentRun.task_id.in_(task_ids))
        .order_by(AgentRun.task_id, AgentRun.created_at)
    )
    latest: dict[UUID, str] = {}
    for tid, run_status in rows.all():
        if tid is not None:
            latest[tid] = run_status  # later rows overwrite: last write wins
    return latest


async def agent_names(
    db: AsyncSession, workspace_id: UUID, agent_ids: list[UUID]
) -> dict[UUID, str]:
    if not agent_ids:
        return {}
    rows = await db.execute(
        select(Agent.id, Agent.name).where(
            Agent.workspace_id == workspace_id, Agent.id.in_(agent_ids)
        )
    )
    return {row[0]: row[1] for row in rows.all()}


async def list_task_runs(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> list[AgentRun]:
    rows = await db.scalars(
        select(AgentRun)
        .where(AgentRun.task_id == task_id, AgentRun.workspace_id == workspace_id)
        .order_by(AgentRun.created_at)
    )
    return list(rows)


async def list_task_events(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> list[RunEvent]:
    """Timeline across all runs of a task, in execution order."""
    rows = await db.scalars(
        select(RunEvent)
        .where(RunEvent.task_id == task_id, RunEvent.workspace_id == workspace_id)
        .order_by(RunEvent.created_at, RunEvent.seq)
    )
    return list(rows)


async def list_task_messages(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> list[Message]:
    rows = await db.scalars(
        select(Message)
        .where(
            Message.task_id == task_id,
            Message.workspace_id == workspace_id,
            Message.visibility == MessageVisibility.VISIBLE.value,
        )
        .order_by(Message.created_at, Message.id)
    )
    return list(rows)


async def list_runs(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    status_filter: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AgentRun], int]:
    limit = min(limit, MAX_PAGE_SIZE)
    query = select(AgentRun).where(AgentRun.workspace_id == workspace_id)
    if status_filter:
        query = query.where(AgentRun.status == status_filter)
    if agent_id is not None:
        query = query.where(AgentRun.agent_id == agent_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.scalars(
        query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), int(total)


async def get_run(db: AsyncSession, workspace_id: UUID, run_id: UUID) -> AgentRun:
    run = await db.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.workspace_id == workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


async def list_run_events(db: AsyncSession, workspace_id: UUID, run_id: UUID) -> list[RunEvent]:
    rows = await db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.workspace_id == workspace_id)
        .order_by(RunEvent.seq)
    )
    return list(rows)


async def list_run_tool_calls(db: AsyncSession, workspace_id: UUID, run_id: UUID) -> list[ToolCall]:
    rows = await db.scalars(
        select(ToolCall)
        .where(ToolCall.run_id == run_id, ToolCall.workspace_id == workspace_id)
        .order_by(ToolCall.created_at, ToolCall.id)
    )
    return list(rows)


# --- Signals ---


async def _workflow_handle(temporal: TemporalClient, task: Task) -> WorkflowHandle[Any, Any]:
    if task.temporal_workflow_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task has no workflow (it was never assigned to an agent)",
        )
    return temporal.get_workflow_handle(task.temporal_workflow_id)


async def signal_task(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    task_id: UUID,
    *,
    signal: str,
    args: list[Any] | None = None,
    action: str,
    request_id: UUID,
    ip_hash: str,
) -> Task:
    task = await get_task(db, ctx.workspace_id, task_id)
    if task.state not in ACTIVE_TASK_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task is already {task.state}",
        )
    handle = await _workflow_handle(temporal, task)
    try:
        await handle.signal(signal, *(args or []))
    except (RPCError, TemporalError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not signal the task workflow (it may have already finished)",
        ) from exc
    audit.record(
        db,
        action=action,
        target_type="task",
        target_id=task.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await db.commit()
    return task


async def send_instruction(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    task_id: UUID,
    *,
    text: str,
    request_id: UUID,
    ip_hash: str,
) -> Task:
    """Persist the user's instruction as a message, then signal the workflow."""
    task = await get_task(db, ctx.workspace_id, task_id)
    if task.state not in ACTIVE_TASK_STATES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Task is {task.state}")
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=task.assigned_agent_id,
            message_type="instruction",
            content_json={"text": text},
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    await db.commit()
    return await signal_task(
        db,
        ctx,
        temporal,
        task_id,
        signal="user_instruction",
        args=[text],
        action="task.instruction",
        request_id=request_id,
        ip_hash=ip_hash,
    )
