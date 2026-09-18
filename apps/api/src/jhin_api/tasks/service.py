"""Task, run, and message business logic (plan 6.12-6.14, 8.3, 19).

The API starts AgentTaskWorkflow through the Temporal client and signals it
for pause/resume/cancel/instruction. Postgres rows (task, agent_run,
message, run_event) are the source of truth the UI reads; the workflow's
activities on the agent worker write them.

Ordering rule for starts: the task row (and any conversational message) is
committed *before* the workflow starts, so activities always find it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from jhin_api.human_authority import human_content
from jhin_db.models import (
    Agent,
    AgentRun,
    Conversation,
    Message,
    RunEvent,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import (
    AgentStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_secrets import SecretCrypto
from jhin_secrets.intake import (
    CapturedInput,
    capture_input,
    merge_capture_metadata,
    safe_input_text,
    secret_spans,
)
from jhin_secrets.variables import VariableError
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskInput

MAX_PAGE_SIZE = 200
# Timeline endpoints return a whole run/task history in one response rather
# than a page. That is fine for a normal task and unbounded for a long-running
# agent loop, so a hard ceiling is applied — high enough never to truncate real
# usage, low enough that one request cannot exhaust the API's memory.
MAX_TIMELINE_ROWS = 2_000
ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)
# metadata_json key stamped when a person dismisses a failed task from the
# attention inbox; the failure stays on the task, it just stops nagging.
ATTENTION_ACKNOWLEDGED_KEY = "attention_acknowledged_at"

# The failure class for a turn that never reached Temporal. It picks the
# sentence a person reads (:mod:`jhin_domain.failures`); the text stored
# beside it is the record, in the shape the agent worker's own failure rows
# use so one reader handles both.
WORKFLOW_START_FAILED_CODE = "workflow_start_failed"
_WORKFLOW_START_FAILED_DETAIL = "Could not start the task workflow (Temporal unavailable)"


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


def record_start_failure(db: AsyncSession, task: Task) -> None:
    """Leave a turn that never started where the person who asked will see it.

    A task marked failed here has no run, so nothing the agent worker writes
    ever happens: no run row, and therefore none of the failure projection
    that normally puts an ``error`` message in the transcript. Without this
    the chat shows the person's message and then nothing at all — which reads
    as an agent still thinking, on the exact stroke of bad luck (a redeploy
    between the commit and the start) that this whole change is about. The
    503 tells whoever made the request; this tells whoever was in the
    conversation, and gives the failure card something to hang the "Try
    again" on.

    Only for a task inside a conversation. A task created straight through
    the API has no transcript for this to appear in, and its caller already
    has the 503.

    The caller owns the transaction.
    """
    if task.conversation_id is None:
        return
    db.add(
        Message(
            workspace_id=task.workspace_id,
            task_id=task.id,
            conversation_id=task.conversation_id,
            sender_type=SenderType.SYSTEM.value,
            sender_id=None,
            recipient_type=RecipientType.TASK.value,
            recipient_id=task.id,
            message_type=MessageType.ERROR.value,
            content_json={
                "text": f"Run failed: {_WORKFLOW_START_FAILED_DETAIL}",
                "error_code": WORKFLOW_START_FAILED_CODE,
            },
            visibility=MessageVisibility.VISIBLE.value,
        )
    )


async def start_workflow(
    db: AsyncSession, temporal: TemporalClient, task: Task, agent_id: UUID, instruction: str
) -> None:
    """Start AgentTaskWorkflow for a committed task row.

    On failure the task is marked failed (it was already committed), the
    conversation is told in words a person can read, and the caller receives
    503 — nothing is left silently queued, and nothing is left invisible.
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
        record_start_failure(db, task)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_WORKFLOW_START_FAILED_DETAIL,
        ) from exc
    task.temporal_workflow_id = workflow_id
    await db.commit()


async def _secure_task_input(
    db: AsyncSession,
    ctx: WorkspaceContext,
    crypto: SecretCrypto | None,
    *,
    agent_id: UUID | None,
    conversation_id: UUID | None,
    text: str,
    title: str = "",
) -> tuple[UUID | None, CapturedInput]:
    title_values = [{"value": title[start:end]} for start, end, _ in secret_spans(title)]
    if not title_values and not secret_spans(text):
        return conversation_id, CapturedInput(text)
    if agent_id is None:
        raise HTTPException(422, "Choose an agent before providing credentials")
    if crypto is None:
        raise HTTPException(503, "Secure storage is unavailable; configure encryption first")
    await db.scalar(
        select(Workspace.id).where(Workspace.id == ctx.workspace_id).with_for_update(key_share=True)
    )
    if conversation_id is None:
        chat = Conversation(
            workspace_id=ctx.workspace_id,
            primary_agent_id=agent_id,
            created_by_user_id=ctx.user.id,
            title=safe_input_text(title or text)[:120],
            last_activity_at=datetime.now(UTC),
        )
        db.add(chat)
        await db.flush()
        conversation_id = chat.id
    else:
        await db.scalar(
            select(Conversation.id)
            .where(
                Conversation.id == conversation_id, Conversation.workspace_id == ctx.workspace_id
            )
            .with_for_update()
        )
    try:
        captured = await capture_input(
            db,
            crypto,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            user_id=ctx.user.id,
            text=text,
            secure_inputs=title_values,
        )
    except VariableError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    return conversation_id, captured


async def create_task(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
    crypto: SecretCrypto | None = None,
) -> Task:
    agent_id: UUID | None = values.pop("agent_id", None)
    if agent_id is not None:
        await _get_active_agent(db, ctx.workspace_id, agent_id)
    conversation_id, captured = await _secure_task_input(
        db,
        ctx,
        crypto,
        agent_id=agent_id,
        conversation_id=None,
        title=values["title"],
        text=values.get("description", ""),
    )

    task = Task(
        workspace_id=ctx.workspace_id,
        title=safe_input_text(values["title"]),
        description=captured.text,
        priority=values["priority"].value,
        assigned_agent_id=agent_id,
        correlation_id=new_uuid7(),
        conversation_id=conversation_id,
        metadata_json=merge_capture_metadata({}, captured),
    )
    db.add(task)
    await db.flush()
    if agent_id is not None:
        db.add(
            Message(
                workspace_id=ctx.workspace_id,
                conversation_id=conversation_id,
                task_id=task.id,
                sender_type=SenderType.USER.value,
                sender_id=ctx.user.id,
                recipient_type=RecipientType.AGENT.value,
                recipient_id=agent_id,
                content_json=human_content(
                    ctx,
                    {
                        "text": task.title + "\n" + captured.text,
                        "secure_inputs": captured.references,
                    },
                ),
                visibility=MessageVisibility.VISIBLE.value,
            )
        )
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
        await start_workflow(db, temporal, task, agent_id, task.description)
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
    crypto: SecretCrypto | None = None,
) -> Task:
    values["agent_id"] = agent_id
    return await create_task(
        db, ctx, temporal, values=values, request_id=request_id, ip_hash=ip_hash, crypto=crypto
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
    crypto: SecretCrypto | None = None,
) -> Task:
    """Conversational entry point (plan 17.5): message → task + run.

    Legacy surface kept for compatibility: it now also opens a first-class
    conversation and links the task and seed message to it.
    """
    agent = await _get_active_agent(db, ctx.workspace_id, agent_id)

    safe_text = safe_input_text(text)
    first_line = next((line for line in safe_text.splitlines() if line.strip()), "")
    title = first_line.strip()[:120] or f"Message to {agent.name}"
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        title=title,
        primary_agent_id=agent_id,
        created_by_user_id=ctx.user.id,
        last_activity_at=datetime.now(UTC),
    )
    db.add(conversation)
    await db.flush()
    _, captured = await _secure_task_input(
        db, ctx, crypto, agent_id=agent_id, conversation_id=conversation.id, text=text
    )
    text = captured.text
    task = Task(
        workspace_id=ctx.workspace_id,
        title=title,
        description=text,
        assigned_agent_id=agent_id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
        metadata_json=merge_capture_metadata(
            {"origin": "message", "conversation_id": str(conversation.id)}, captured
        ),
    )
    db.add(task)
    await db.flush()
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            conversation_id=conversation.id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=agent_id,
            content_json=human_content(ctx, {"text": text, "secure_inputs": captured.references}),
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
        metadata={"task_id": str(task.id), "conversation_id": str(conversation.id)},
    )
    audit.record(
        db,
        action="conversation.created",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"agent_id": str(agent_id), "title": title, "origin": "message"},
    )
    await db.commit()

    await start_workflow(db, temporal, task, agent_id, text)
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
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    offset = max(offset, 0)
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


@dataclass(frozen=True, slots=True)
class LatestRun:
    """The newest run of one task, as the surfaces watching it need it."""

    #: The run itself, so a caller can go on to read what happened *inside* it
    #: — the approvals and questions it stopped on, which is how long it
    #: waited rather than how long it has existed.
    id: UUID
    status: str
    #: When this run began. ``started_at`` where the worker stamped it, and
    #: the row's own creation where it has not yet — a run row exists a beat
    #: before it is marked started, and a caller counting a wait needs an
    #: answer for that beat rather than a gap.
    started_at: datetime | None
    #: When this run finished, or ``None`` while it is still in flight. The
    #: other half of the span: an approval or a question points at a run
    #: without being bounded by it, so a caller measuring waits needs to know
    #: where the run stopped in order to ignore the ones stamped after it.
    completed_at: datetime | None = None


async def latest_run_by_task(
    db: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
) -> dict[UUID, LatestRun]:
    """Latest run per task: one query, whatever the caller reads off it."""
    if not task_ids:
        return {}
    rows = await db.execute(
        select(
            AgentRun.task_id,
            AgentRun.id,
            AgentRun.status,
            AgentRun.started_at,
            AgentRun.created_at,
            AgentRun.completed_at,
        )
        .where(AgentRun.workspace_id == workspace_id, AgentRun.task_id.in_(task_ids))
        .order_by(AgentRun.task_id, AgentRun.created_at)
    )
    latest: dict[UUID, LatestRun] = {}
    for tid, run_id, run_status, started_at, created_at, completed_at in rows.all():
        if tid is not None:
            # Later rows overwrite: last write wins.
            latest[tid] = LatestRun(
                id=run_id,
                status=run_status,
                started_at=started_at or created_at,
                completed_at=completed_at,
            )
    return latest


async def latest_run_status_by_task(
    db: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
) -> dict[UUID, str]:
    """Latest run status per task, for tree/lineage displays."""
    return {
        tid: run.status
        for tid, run in (await latest_run_by_task(db, workspace_id, task_ids)).items()
    }


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
        .limit(MAX_TIMELINE_ROWS)
    )
    return list(rows)


async def list_task_events(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> list[RunEvent]:
    """Timeline across all runs of a task, in execution order."""
    rows = await db.scalars(
        select(RunEvent)
        .where(RunEvent.task_id == task_id, RunEvent.workspace_id == workspace_id)
        .order_by(RunEvent.created_at, RunEvent.seq)
        .limit(MAX_TIMELINE_ROWS)
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
        .limit(MAX_TIMELINE_ROWS)
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
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    offset = max(offset, 0)
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
        .limit(MAX_TIMELINE_ROWS)
    )
    return list(rows)


async def list_run_tool_calls(db: AsyncSession, workspace_id: UUID, run_id: UUID) -> list[ToolCall]:
    rows = await db.scalars(
        select(ToolCall)
        .where(ToolCall.run_id == run_id, ToolCall.workspace_id == workspace_id)
        .order_by(ToolCall.created_at, ToolCall.id)
        .limit(MAX_TIMELINE_ROWS)
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
    if signal == "cancel":
        task.metadata_json = {
            **task.metadata_json,
            "stop_requested_at": datetime.now(UTC).isoformat(),
        }
        await db.commit()
        from jhin_api.runtime.service import cancel_task_invocations

        await cancel_task_invocations(db, ctx, task_id)
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


def is_attention_acknowledged(task: Task) -> bool:
    return bool((task.metadata_json or {}).get(ATTENTION_ACKNOWLEDGED_KEY))


def mark_attention_acknowledged(task: Task, *, at: datetime | None = None) -> bool:
    """Stamp the task in memory; returns False when it was already stamped.
    The caller owns the transaction (and the audit row)."""
    if is_attention_acknowledged(task):
        return False
    stamped = dict(task.metadata_json or {})
    stamped[ATTENTION_ACKNOWLEDGED_KEY] = (at or datetime.now(UTC)).isoformat()
    task.metadata_json = stamped
    return True


async def acknowledge_task(
    db: AsyncSession,
    ctx: WorkspaceContext,
    task_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> Task:
    """A member dismisses a failed task from the attention inbox. Idempotent:
    a second call returns the task unchanged without a second audit row."""
    task = await get_task(db, ctx.workspace_id, task_id)
    if task.state != TaskState.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed tasks can be dismissed (task is {task.state})",
        )
    if mark_attention_acknowledged(task):
        audit.record(
            db,
            action="task.acknowledged",
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
    crypto: SecretCrypto | None = None,
) -> Task:
    """Persist the user's instruction as a message, then signal the workflow."""
    task = await get_task(db, ctx.workspace_id, task_id)
    if task.state not in ACTIVE_TASK_STATES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Task is {task.state}")
    conversation_id, captured = await _secure_task_input(
        db,
        ctx,
        crypto,
        agent_id=task.assigned_agent_id,
        conversation_id=task.conversation_id,
        text=text,
    )
    task.conversation_id = conversation_id
    task.metadata_json = merge_capture_metadata(task.metadata_json or {}, captured)
    text = captured.text
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            conversation_id=conversation_id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=task.assigned_agent_id,
            message_type=MessageType.INSTRUCTION.value,
            content_json=human_content(ctx, {"text": text, "secure_inputs": captured.references}),
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
