"""Conversations, turns, the company activity feed, and the attention summary.

A conversation is a named thread between a human and one primary agent. Each
user turn that needs agent work becomes a ``task`` linked to the conversation
(``AgentTaskWorkflow`` runs unchanged); a turn sent while a task is still
active becomes a ``user_instruction`` signal instead. See
``docs/architecture/conversations.md`` for the contract this module implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from jhin_api.approvals.schemas import ApprovalOut
from jhin_api.audit import service as audit
from jhin_api.conversations.schemas import (
    AcknowledgeFailuresOut,
    ActivityCardOut,
    ActivityListOut,
    AttentionCounts,
    AttentionOut,
    ConversationAgentOut,
    ConversationDetailOut,
    ConversationMessageOut,
    ConversationOut,
)
from jhin_api.coordination import service as coordination
from jhin_api.deps import WorkspaceContext
from jhin_api.public_payloads import public_tool_payload
from jhin_api.tasks import service as tasks_service
from jhin_api.tasks.schemas import TaskOut
from jhin_db.models import (
    Agent,
    AgentRun,
    AgentTeamMembership,
    Approval,
    Conversation,
    Message,
    Task,
    User,
    WorkRequest,
    WorkReview,
)
from jhin_domain import (
    ACTIVITY_LABELS,
    AGENT_MESSAGE_TYPES,
    ActivityKind,
    AgentStatus,
    ApprovalStatus,
    ConversationStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    ReviewerType,
    RunStatus,
    SenderType,
    TaskState,
    WorkRequestStatus,
    WorkReviewStatus,
    new_uuid7,
)
from jhin_events import EventEnvelope, EventPublisher, EventSource
from jhin_observability import get_logger, normalize_event_family

logger = get_logger(__name__)

MAX_PAGE_SIZE = 100
PREVIEW_CHARS = 160
SUMMARY_CHARS = 400
DEFAULT_TITLE_CHARS = 120
FAILED_TASK_WINDOW = timedelta(days=7)
ROOT_WALK_MAX_DEPTH = 20

TurnMode = Literal["new_task", "instruction"]

# Structured agent-to-agent messages that become feed cards (plan 29 minus
# human instructions, which are user turns rather than company traffic).
_FEED_MESSAGE_KINDS: dict[str, ActivityKind] = {
    MessageType.DELEGATION.value: ActivityKind.ASKED_AGENT,
    MessageType.REVIEW_REQUEST.value: ActivityKind.ASKED_AGENT,
    MessageType.QUESTION.value: ActivityKind.ASKED_AGENT,
    MessageType.RESULT.value: ActivityKind.REPORTED,
    MessageType.REVIEW_RESULT.value: ActivityKind.REPORTED,
    MessageType.ESCALATION.value: ActivityKind.ESCALATED,
    MessageType.STATUS.value: ActivityKind.STATUS_UPDATE,
}
assert set(_FEED_MESSAGE_KINDS) == {t.value for t in AGENT_MESSAGE_TYPES} - {
    MessageType.INSTRUCTION.value
}

_TASK_LIFECYCLE_KINDS: dict[str, ActivityKind] = {
    TaskState.COMPLETED.value: ActivityKind.FINISHED,
    TaskState.FAILED.value: ActivityKind.FAILED,
    TaskState.PAUSED.value: ActivityKind.PAUSED,
    TaskState.CANCELLED.value: ActivityKind.STOPPED,
}
_TASK_KINDS = frozenset(
    {ActivityKind.STARTED, ActivityKind.QUEUED, *_TASK_LIFECYCLE_KINDS.values()}
)


@dataclass(frozen=True)
class TurnResult:
    conversation: Conversation
    message: Message
    task: Task
    mode: TurnMode


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive timestamps; treat them as UTC for ordering."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _message_text(content: dict[str, Any]) -> str:
    for key in ("text", "summary"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def default_title(text: str | None, agent_name: str) -> str:
    first_line = next((line for line in (text or "").splitlines() if line.strip()), "")
    return first_line.strip()[:DEFAULT_TITLE_CHARS] or f"Chat with {agent_name}"


async def _publish(
    publisher: EventPublisher | None, workspace_id: UUID, event_type: str, data: dict[str, Any]
) -> None:
    """Best-effort backbone publish; Postgres already holds the fact."""
    if publisher is None:
        return
    try:
        await publisher.publish(
            EventEnvelope(
                event_type=event_type,
                workspace_id=str(workspace_id),
                source=EventSource(type="api"),
                data=data,
            )
        )
    except Exception as exc:
        logger.warning(
            "events.publish_failed",
            event_type=normalize_event_family(event_type),
            error_type=type(exc).__name__,
        )


# --- Lookups ---


async def get_conversation(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID
) -> Conversation:
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if conversation is None:
        raise _not_found()
    return conversation


async def _get_agent(db: AsyncSession, workspace_id: UUID, agent_id: UUID | None) -> Agent | None:
    if agent_id is None:
        return None
    agent: Agent | None = await db.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    return agent


async def _require_active_agent(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID | None
) -> Agent:
    agent = await _get_agent(db, workspace_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.status != AgentStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"This agent is {agent.status}"
        )
    return agent


async def _conversation_tasks(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID
) -> list[Task]:
    """Tasks in one conversation, newest first."""
    rows = await db.scalars(
        select(Task)
        .where(Task.workspace_id == workspace_id, Task.conversation_id == conversation_id)
        .order_by(Task.created_at.desc(), Task.id.desc())
    )
    return list(rows)


def _active_task(tasks: list[Task]) -> Task | None:
    """Most recent queued/running/paused task (``tasks`` is newest first)."""
    return next((t for t in tasks if t.state in tasks_service.ACTIVE_TASK_STATES), None)


# --- Projection ---


async def project_conversations(
    db: AsyncSession, workspace_id: UUID, conversations: list[Conversation]
) -> list[ConversationOut]:
    if not conversations:
        return []
    ids = [c.id for c in conversations]
    tasks_by_conversation: dict[UUID, list[Task]] = {cid: [] for cid in ids}
    all_tasks = list(
        await db.scalars(
            select(Task)
            .where(Task.workspace_id == workspace_id, Task.conversation_id.in_(ids))
            .order_by(Task.created_at.desc(), Task.id.desc())
        )
    )
    for task in all_tasks:
        if task.conversation_id is not None:
            tasks_by_conversation[task.conversation_id].append(task)
    active_tasks = {
        cid: active
        for cid, tasks in tasks_by_conversation.items()
        if (active := _active_task(tasks)) is not None
    }
    run_status = await tasks_service.latest_run_status_by_task(
        db, workspace_id, [t.id for t in active_tasks.values()]
    )
    agent_rows = await db.execute(
        select(Agent.id, Agent.name, Agent.role_title).where(
            Agent.workspace_id == workspace_id,
            Agent.id.in_([c.primary_agent_id for c in conversations if c.primary_agent_id]),
        )
    )
    agents = {row[0]: (row[1], row[2]) for row in agent_rows.all()}

    out: list[ConversationOut] = []
    for conversation in conversations:
        tasks = tasks_by_conversation[conversation.id]
        task_ids = [t.id for t in tasks]
        last_message = await db.scalar(
            select(Message)
            .where(
                Message.workspace_id == workspace_id,
                Message.visibility == MessageVisibility.VISIBLE.value,
                or_(
                    Message.conversation_id == conversation.id,
                    Message.task_id.in_(task_ids),
                ),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        active = active_tasks.get(conversation.id)
        name, role_title = (None, None)
        if conversation.primary_agent_id is not None:
            name, role_title = agents.get(conversation.primary_agent_id, (None, None))
        out.append(
            ConversationOut(
                id=conversation.id,
                workspace_id=conversation.workspace_id,
                title=conversation.title,
                status=conversation.status,
                pinned=conversation.pinned,
                primary_agent_id=conversation.primary_agent_id,
                created_by_user_id=conversation.created_by_user_id,
                last_activity_at=conversation.last_activity_at,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                active_task_id=active.id if active else None,
                active_task_state=active.state if active else None,
                active_run_status=run_status.get(active.id) if active else None,
                last_message_preview=(
                    _truncate(_message_text(last_message.content_json), PREVIEW_CHARS) or None
                    if last_message is not None
                    else None
                ),
                last_message_sender_type=(
                    last_message.sender_type if last_message is not None else None
                ),
                agent_name=name,
                agent_role_title=role_title,
                task_count=len(tasks),
            )
        )
    return out


async def project_conversation(
    db: AsyncSession, workspace_id: UUID, conversation: Conversation
) -> ConversationOut:
    return (await project_conversations(db, workspace_id, [conversation]))[0]


async def project_messages(
    db: AsyncSession, workspace_id: UUID, messages: list[Message]
) -> list[ConversationMessageOut]:
    sender_ids = {m.sender_id for m in messages if m.sender_id is not None}
    agent_names = await tasks_service.agent_names(db, workspace_id, list(sender_ids))
    user_ids = [
        m.sender_id
        for m in messages
        if m.sender_id is not None and m.sender_type == SenderType.USER.value
    ]
    user_names: dict[UUID, str] = {}
    if user_ids:
        rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))
        user_names = {row[0]: row[1] for row in rows.all()}
    out: list[ConversationMessageOut] = []
    for message in messages:
        sender_name: str | None
        agent_id: UUID | None = None
        if message.sender_type == SenderType.AGENT.value:
            agent_id = message.sender_id
            sender_name = agent_names.get(message.sender_id) if message.sender_id else None
        elif message.sender_type == SenderType.USER.value:
            sender_name = user_names.get(message.sender_id) if message.sender_id else None
        else:
            sender_name = "System"
        out.append(
            ConversationMessageOut(
                id=message.id,
                task_id=message.task_id,
                run_id=message.run_id,
                sender_type=message.sender_type,
                sender_id=message.sender_id,
                message_type=message.message_type,
                content_json=message.content_json,
                created_at=message.created_at,
                conversation_id=message.conversation_id,
                sender_name=sender_name,
                agent_id=agent_id,
            )
        )
    return out


# --- Reads ---


async def list_conversations(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    q: str | None = None,
    agent_id: UUID | None = None,
    status_filter: str | None = None,
    pinned: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Conversation], int]:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    query = select(Conversation).where(Conversation.workspace_id == workspace_id)
    status_value = status_filter or ConversationStatus.ACTIVE.value
    query = query.where(Conversation.status == status_value)
    if q:
        query = query.where(Conversation.title.ilike(f"%{q.strip()}%"))
    if agent_id is not None:
        query = query.where(Conversation.primary_agent_id == agent_id)
    if pinned is not None:
        query = query.where(Conversation.pinned.is_(pinned))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.scalars(
        query.order_by(
            Conversation.pinned.desc(),
            Conversation.last_activity_at.desc(),
            Conversation.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total)


async def get_detail(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID
) -> ConversationDetailOut:
    conversation = await get_conversation(db, workspace_id, conversation_id)
    agent = await _get_agent(db, workspace_id, conversation.primary_agent_id)
    tasks = await _conversation_tasks(db, workspace_id, conversation_id)
    task_ids = [t.id for t in tasks]
    runs: list[AgentRun] = []
    approvals: list[Approval] = []
    if task_ids:
        runs = list(
            await db.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id, AgentRun.task_id.in_(task_ids)
                )
            )
        )
        approvals = list(
            await db.scalars(
                select(Approval)
                .where(
                    Approval.workspace_id == workspace_id,
                    Approval.task_id.in_(task_ids),
                    Approval.status == ApprovalStatus.PENDING.value,
                )
                .order_by(Approval.requested_at.desc())
            )
        )
    return ConversationDetailOut(
        conversation=await project_conversation(db, workspace_id, conversation),
        agent=ConversationAgentOut.model_validate(agent) if agent is not None else None,
        tasks=[TaskOut.model_validate(t) for t in tasks],
        total_input_tokens=sum(r.input_tokens for r in runs),
        total_output_tokens=sum(r.output_tokens for r in runs),
        total_cost_micros=sum(r.estimated_cost_micros for r in runs),
        pending_approvals=[ApprovalOut.model_validate(a) for a in approvals],
    )


async def list_messages(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    *,
    after: UUID | None = None,
) -> list[Message]:
    """Visible messages across every task in the conversation, oldest first."""
    await get_conversation(db, workspace_id, conversation_id)
    task_ids = select(Task.id).where(
        Task.workspace_id == workspace_id, Task.conversation_id == conversation_id
    )
    query = select(Message).where(
        Message.workspace_id == workspace_id,
        Message.visibility == MessageVisibility.VISIBLE.value,
        or_(Message.conversation_id == conversation_id, Message.task_id.in_(task_ids)),
    )
    if after is not None:
        anchor = await db.scalar(
            select(Message).where(Message.id == after, Message.workspace_id == workspace_id)
        )
        if anchor is not None:
            query = query.where(
                or_(
                    Message.created_at > anchor.created_at,
                    and_(Message.created_at == anchor.created_at, Message.id > anchor.id),
                )
            )
    rows = await db.scalars(query.order_by(Message.created_at, Message.id))
    return list(rows)


# --- Writes ---


async def create_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    *,
    agent_id: UUID,
    title: str | None,
    text: str | None,
    client_turn_id: str | None,
    request_id: UUID,
    ip_hash: str,
    publisher: EventPublisher | None = None,
) -> tuple[Conversation, TurnResult | None]:
    agent = await _require_active_agent(db, ctx.workspace_id, agent_id)
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        title=(title or "").strip()[:200] or default_title(text, agent.name),
        primary_agent_id=agent.id,
        created_by_user_id=ctx.user.id,
        last_activity_at=_now(),
    )
    db.add(conversation)
    await db.flush()
    audit.record(
        db,
        action="conversation.created",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"agent_id": str(agent.id), "title": conversation.title},
    )
    await db.commit()
    await _publish(
        publisher,
        ctx.workspace_id,
        "conversation.created",
        {"conversation_id": str(conversation.id), "agent_id": str(agent.id)},
    )
    turn: TurnResult | None = None
    if text is not None:
        turn = await _run_turn(
            db,
            ctx,
            temporal,
            conversation,
            agent,
            text=text,
            client_turn_id=client_turn_id,
            request_id=request_id,
            ip_hash=ip_hash,
            publisher=publisher,
        )
    return conversation, turn


async def update_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    *,
    values: dict[str, Any],
    request_id: UUID,
    ip_hash: str,
) -> Conversation:
    conversation = await get_conversation(db, ctx.workspace_id, conversation_id)
    changed: dict[str, Any] = {}
    if (title := values.get("title")) is not None:
        conversation.title = title.strip()[:200] or conversation.title
        changed["title"] = conversation.title
    if (pinned := values.get("pinned")) is not None:
        conversation.pinned = bool(pinned)
        changed["pinned"] = conversation.pinned
    if (new_status := values.get("status")) is not None:
        conversation.status = ConversationStatus(new_status).value
        changed["status"] = conversation.status
    audit.record(
        db,
        action="conversation.updated",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata=changed,
    )
    await db.commit()
    return conversation


async def delete_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    """Delete the thread; tasks and messages keep their rows (FK set null)."""
    conversation = await get_conversation(db, ctx.workspace_id, conversation_id)
    for task in await _conversation_tasks(db, ctx.workspace_id, conversation_id):
        task.conversation_id = None
    messages = await db.scalars(
        select(Message).where(
            Message.workspace_id == ctx.workspace_id, Message.conversation_id == conversation_id
        )
    )
    for message in messages:
        message.conversation_id = None
    audit.record(
        db,
        action="conversation.deleted",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"title": conversation.title},
    )
    await db.delete(conversation)
    await db.commit()


async def send_turn(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    conversation_id: UUID,
    *,
    text: str,
    client_turn_id: str | None,
    request_id: UUID,
    ip_hash: str,
    publisher: EventPublisher | None = None,
) -> TurnResult:
    conversation = await get_conversation(db, ctx.workspace_id, conversation_id)
    if conversation.status != ConversationStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This conversation is archived"
        )
    agent = await _require_active_agent(db, ctx.workspace_id, conversation.primary_agent_id)
    return await _run_turn(
        db,
        ctx,
        temporal,
        conversation,
        agent,
        text=text,
        client_turn_id=client_turn_id,
        request_id=request_id,
        ip_hash=ip_hash,
        publisher=publisher,
    )


async def _existing_turn(
    db: AsyncSession, conversation: Conversation, client_turn_id: str
) -> TurnResult | None:
    message = await db.scalar(
        select(Message)
        .where(
            Message.workspace_id == conversation.workspace_id,
            Message.conversation_id == conversation.id,
            Message.sender_type == SenderType.USER.value,
            Message.content_json["client_turn_id"].as_string() == client_turn_id,
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    if message is None or message.task_id is None:
        return None
    task = await db.get(Task, message.task_id)
    if task is None:
        return None
    mode: TurnMode = (
        "instruction" if message.message_type == MessageType.INSTRUCTION.value else "new_task"
    )
    return TurnResult(conversation=conversation, message=message, task=task, mode=mode)


async def _run_turn(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    conversation: Conversation,
    agent: Agent,
    *,
    text: str,
    client_turn_id: str | None,
    request_id: UUID,
    ip_hash: str,
    publisher: EventPublisher | None,
) -> TurnResult:
    if client_turn_id:
        existing = await _existing_turn(db, conversation, client_turn_id)
        if existing is not None:
            return existing

    content: dict[str, Any] = {"text": text}
    if client_turn_id:
        content["client_turn_id"] = client_turn_id
    tasks = await _conversation_tasks(db, ctx.workspace_id, conversation.id)
    active = _active_task(tasks)

    if active is not None:
        message = Message(
            workspace_id=ctx.workspace_id,
            task_id=active.id,
            conversation_id=conversation.id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=agent.id,
            message_type=MessageType.INSTRUCTION.value,
            content_json=content,
            visibility=MessageVisibility.VISIBLE.value,
        )
        db.add(message)
        await db.commit()
        task = await tasks_service.signal_task(
            db,
            ctx,
            temporal,
            active.id,
            signal="user_instruction",
            args=[text],
            action="task.instruction",
            request_id=request_id,
            ip_hash=ip_hash,
        )
        mode: TurnMode = "instruction"
    else:
        # Each work episode is titled after the message that started it, so
        # activity and the details panel describe what was actually asked
        # rather than repeating the chat's (first-message) title.
        task = Task(
            workspace_id=ctx.workspace_id,
            title=default_title(text, agent.name)[:500],
            description=text,
            assigned_agent_id=agent.id,
            conversation_id=conversation.id,
            correlation_id=new_uuid7(),
            metadata_json={"origin": "conversation", "conversation_id": str(conversation.id)},
        )
        db.add(task)
        await db.flush()
        message = Message(
            workspace_id=ctx.workspace_id,
            task_id=task.id,
            conversation_id=conversation.id,
            sender_type=SenderType.USER.value,
            sender_id=ctx.user.id,
            recipient_type=RecipientType.AGENT.value,
            recipient_id=agent.id,
            message_type=MessageType.TEXT.value,
            content_json=content,
            visibility=MessageVisibility.VISIBLE.value,
        )
        db.add(message)
        mode = "new_task"

    conversation.last_activity_at = _now()
    audit.record(
        db,
        action="conversation.turn",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"task_id": str(task.id), "mode": mode, "message_id": str(message.id)},
    )
    await db.commit()
    if mode == "new_task":
        # Commit before start so the worker's activities always find the row.
        await tasks_service.start_workflow(db, temporal, task, agent.id, text)
    await _publish(
        publisher,
        ctx.workspace_id,
        "conversation.turn",
        {
            "conversation_id": str(conversation.id),
            "task_id": str(task.id),
            "message_id": str(message.id),
            "mode": mode,
        },
    )
    return TurnResult(conversation=conversation, message=message, task=task, mode=mode)


# --- Activity feed ---


def parse_kinds(raw: str | None) -> set[ActivityKind] | None:
    if not raw:
        return None
    kinds: set[ActivityKind] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            kinds.add(ActivityKind(token))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown activity kind '{token}'",
            ) from exc
    return kinds or None


async def _team_agent_ids(db: AsyncSession, workspace_id: UUID, team_id: UUID) -> set[UUID]:
    legacy = await db.scalars(
        select(Agent.id).where(Agent.workspace_id == workspace_id, Agent.team_id == team_id)
    )
    members = await db.scalars(
        select(AgentTeamMembership.agent_id).where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.team_id == team_id,
            AgentTeamMembership.left_at.is_(None),
        )
    )
    return set(legacy) | set(members)


async def _conversation_scope_task_ids(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID
) -> set[UUID]:
    """Tasks in the conversation plus every delegated descendant."""
    scope = {t.id for t in await _conversation_tasks(db, workspace_id, conversation_id)}
    frontier = list(scope)
    for _ in range(ROOT_WALK_MAX_DEPTH):
        if not frontier:
            break
        children = list(
            await db.scalars(
                select(Task.id).where(
                    Task.workspace_id == workspace_id, Task.parent_task_id.in_(frontier)
                )
            )
        )
        frontier = [cid for cid in children if cid not in scope]
        scope.update(frontier)
    return scope


async def _task_lineage(
    db: AsyncSession, workspace_id: UUID, task_ids: set[UUID]
) -> dict[UUID, Task]:
    """Load the given tasks and every ancestor (bounded depth)."""
    loaded: dict[UUID, Task] = {}
    pending = set(task_ids)
    for _ in range(ROOT_WALK_MAX_DEPTH + 1):
        missing = [tid for tid in pending if tid not in loaded]
        if not missing:
            break
        rows = await db.scalars(
            select(Task).where(Task.workspace_id == workspace_id, Task.id.in_(missing))
        )
        pending = set()
        for task in rows:
            loaded[task.id] = task
            if task.parent_task_id is not None and task.parent_task_id not in loaded:
                pending.add(task.parent_task_id)
    return loaded


def _root_of(task_id: UUID, lineage: dict[UUID, Task]) -> Task | None:
    current = lineage.get(task_id)
    for _ in range(ROOT_WALK_MAX_DEPTH):
        if current is None or current.parent_task_id is None:
            break
        parent = lineage.get(current.parent_task_id)
        if parent is None:
            break
        current = parent
    return current


def _as_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


@dataclass
class _CardDraft:
    id: str
    kind: ActivityKind
    actor_type: Literal["agent", "user", "system"]
    created_at: datetime
    summary: str
    actor_agent_id: UUID | None = None
    target_agent_id: UUID | None = None
    target_agent_name: str | None = None
    task_id: UUID | None = None
    approval_id: UUID | None = None
    work_request_id: UUID | None = None
    review_id: UUID | None = None
    detail_json: dict[str, Any] | None = None


def _is_work_request_message(message: Message) -> bool:
    """Work-request traffic is projected from the authoritative
    ``work_request`` row instead of its mirror messages (no duplicate cards)."""
    return message.content_json.get("kind") == "work_request"


def _work_request_cards(request: WorkRequest) -> list[_CardDraft]:
    names = request.metadata_json
    target_name = str(names.get("target_agent_name", "") or "") or None
    detail = {
        "status": request.status,
        "title": request.title,
        "expected_output": request.expected_output[:2_000],
        "response": request.response[:2_000],
        "created_task_id": str(request.created_task_id) if request.created_task_id else None,
        "depth": request.depth,
    }
    cards = [
        _CardDraft(
            id=f"work_request:{request.id}:asked",
            kind=ActivityKind.ASKED_AGENT,
            actor_type="agent",
            actor_agent_id=request.requester_agent_id,
            target_agent_id=request.target_agent_id,
            target_agent_name=target_name,
            task_id=request.requester_task_id,
            work_request_id=request.id,
            created_at=request.created_at,
            summary=_truncate(
                f"Asked {target_name or 'another agent'}: {request.title}", SUMMARY_CHARS
            ),
            detail_json=detail,
        )
    ]
    if request.status in (
        WorkRequestStatus.COMPLETED.value,
        WorkRequestStatus.FAILED.value,
        WorkRequestStatus.DECLINED.value,
    ):
        requester_name = str(names.get("requester_agent_name", "") or "") or None
        verb = {
            WorkRequestStatus.COMPLETED.value: "finished",
            WorkRequestStatus.FAILED.value: "could not complete",
            WorkRequestStatus.DECLINED.value: "declined",
        }[request.status]
        cards.append(
            _CardDraft(
                id=f"work_request:{request.id}:reported",
                kind=ActivityKind.REPORTED,
                actor_type="agent",
                actor_agent_id=request.target_agent_id,
                target_agent_id=request.requester_agent_id,
                target_agent_name=requester_name,
                task_id=request.requester_task_id,
                work_request_id=request.id,
                created_at=request.completed_at or request.updated_at,
                summary=_truncate(
                    f"{target_name or 'An agent'} {verb} the request “{request.title}”"
                    + (f": {request.response}" if request.response else "."),
                    SUMMARY_CHARS,
                ),
                detail_json=detail,
            )
        )
    return cards


def _review_card(review: WorkReview) -> _CardDraft:
    summary = str(review.evidence_json.get("summary", "") or "")
    return _CardDraft(
        id=f"review:{review.id}",
        kind=ActivityKind.NEEDS_REVIEW,
        actor_type="agent",
        actor_agent_id=review.subject_agent_id,
        target_agent_id=review.reviewer_agent_id,
        task_id=review.task_id,
        review_id=review.id,
        created_at=review.requested_at,
        summary=_truncate(
            summary
            or f"Review needed ({review.mode.replace('_', ' ')}, {review.reviewer_type} reviewer)",
            SUMMARY_CHARS,
        ),
        detail_json={
            "mode": review.mode,
            "status": review.status,
            "reviewer_type": review.reviewer_type,
            "matched_conditions": review.evidence_json.get("matched_conditions", []),
            "tool_name": review.evidence_json.get("tool_name"),
            "risk": review.evidence_json.get("risk"),
        },
    )


def _message_card(message: Message) -> _CardDraft:
    kind = _FEED_MESSAGE_KINDS[message.message_type]
    content = message.content_json
    target_id: UUID | None = None
    target_name: str | None = None
    if kind is ActivityKind.ASKED_AGENT:
        target_id = _as_uuid(content.get("target_agent_id"))
        name = content.get("target_agent_name")
        target_name = name if isinstance(name, str) and name else None
    if target_id is None and message.recipient_type == RecipientType.AGENT.value:
        target_id = message.recipient_id
    return _CardDraft(
        id=f"msg:{message.id}",
        kind=kind,
        actor_type="agent",
        actor_agent_id=message.sender_id,
        target_agent_id=target_id,
        target_agent_name=target_name,
        task_id=message.task_id,
        created_at=message.created_at,
        summary=_truncate(_message_text(content), SUMMARY_CHARS),
        detail_json=dict(content),
    )


async def _latest_run_errors(
    db: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
) -> dict[UUID, str]:
    """Latest non-empty run error per failed task (already redacted upstream)."""
    if not task_ids:
        return {}
    rows = await db.execute(
        select(AgentRun.task_id, AgentRun.error_message)
        .where(
            AgentRun.workspace_id == workspace_id,
            AgentRun.task_id.in_(task_ids),
            AgentRun.error_message.is_not(None),
        )
        .order_by(AgentRun.task_id, AgentRun.created_at)
    )
    latest: dict[UUID, str] = {}
    for task_id, message in rows.all():
        if task_id is not None and message:
            latest[task_id] = message  # later rows overwrite: last write wins
    return latest


def _task_cards(task: Task) -> list[_CardDraft]:
    detail = {
        "state": task.state,
        "priority": task.priority,
        "origin": task.metadata_json.get("origin"),
    }
    queued = bool(task.metadata_json.get("queue")) and task.state == TaskState.QUEUED.value
    cards = [
        _CardDraft(
            id=f"task:{task.id}:started",
            kind=ActivityKind.QUEUED if queued else ActivityKind.STARTED,
            actor_type="agent",
            actor_agent_id=task.assigned_agent_id,
            task_id=task.id,
            created_at=task.created_at,
            summary="",
            detail_json=detail,
        )
    ]
    lifecycle = _TASK_LIFECYCLE_KINDS.get(task.state)
    if lifecycle is not None:
        cards.append(
            _CardDraft(
                id=f"task:{task.id}:{task.state}",
                kind=lifecycle,
                actor_type="agent",
                actor_agent_id=task.assigned_agent_id,
                task_id=task.id,
                created_at=task.updated_at,
                summary="",
                detail_json=detail,
            )
        )
    return cards


def _approval_card(approval: Approval) -> _CardDraft:
    return _CardDraft(
        id=f"approval:{approval.id}",
        kind=ActivityKind.NEEDS_REVIEW,
        actor_type="agent",
        actor_agent_id=approval.requested_by_agent_id,
        task_id=approval.task_id,
        approval_id=approval.id,
        created_at=approval.requested_at,
        summary=_truncate(
            approval.reason or f"Approval needed for {approval.action_type}", SUMMARY_CHARS
        ),
        detail_json={
            "action_type": approval.action_type,
            "payload": public_tool_payload(approval.action_type, approval.action_payload_sanitized),
        },
    )


_TASK_SUMMARY_TEMPLATES: dict[ActivityKind, str] = {
    ActivityKind.STARTED: "{agent} started working on “{title}”.",
    ActivityKind.QUEUED: "{agent} is waiting for a free slot to work on “{title}”.",
    ActivityKind.FINISHED: "{agent} finished “{title}”.",
    ActivityKind.FAILED: "{agent} ran into a problem with “{title}”.",
    ActivityKind.PAUSED: "{agent} paused “{title}”.",
    ActivityKind.STOPPED: "“{title}” was stopped.",
}


async def list_activity(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: UUID | None = None,
    team_id: UUID | None = None,
    conversation_id: UUID | None = None,
    kinds: set[ActivityKind] | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> ActivityListOut:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    fetch = limit * 2

    agent_filter: set[UUID] | None = None
    if team_id is not None:
        agent_filter = await _team_agent_ids(db, workspace_id, team_id)
    if agent_id is not None:
        agent_filter = {agent_id} if agent_filter is None else (agent_filter & {agent_id})
    scope: set[UUID] | None = None
    if conversation_id is not None:
        await get_conversation(db, workspace_id, conversation_id)
        scope = await _conversation_scope_task_ids(db, workspace_id, conversation_id)

    def _wants(card_kinds: frozenset[ActivityKind]) -> bool:
        return kinds is None or bool(kinds & card_kinds)

    drafts: list[_CardDraft] = []
    empty_scope = scope is not None and not scope
    empty_agents = agent_filter is not None and not agent_filter

    message_kinds = frozenset(_FEED_MESSAGE_KINDS.values())
    if _wants(message_kinds) and not empty_scope and not empty_agents:
        wanted_types = [
            mt for mt, kind in _FEED_MESSAGE_KINDS.items() if kinds is None or kind in kinds
        ]
        query = select(Message).where(
            Message.workspace_id == workspace_id,
            Message.visibility == MessageVisibility.VISIBLE.value,
            Message.sender_type == SenderType.AGENT.value,
            Message.message_type.in_(wanted_types),
        )
        if before is not None:
            query = query.where(Message.created_at < before)
        if scope is not None:
            query = query.where(Message.task_id.in_(scope))
        if agent_filter is not None:
            ids = list(agent_filter)
            query = query.where(or_(Message.sender_id.in_(ids), Message.recipient_id.in_(ids)))
        rows = await db.scalars(
            query.order_by(Message.created_at.desc(), Message.id.desc()).limit(fetch)
        )
        drafts.extend(_message_card(m) for m in rows if not _is_work_request_message(m))

    request_kinds = frozenset({ActivityKind.ASKED_AGENT, ActivityKind.REPORTED})
    if _wants(request_kinds) and not empty_scope and not empty_agents:
        query_w = select(WorkRequest).where(WorkRequest.workspace_id == workspace_id)
        if before is not None:
            query_w = query_w.where(WorkRequest.created_at < before)
        if scope is not None:
            query_w = query_w.where(WorkRequest.requester_task_id.in_(scope))
        if agent_filter is not None:
            ids = list(agent_filter)
            query_w = query_w.where(
                or_(WorkRequest.requester_agent_id.in_(ids), WorkRequest.target_agent_id.in_(ids))
            )
        request_rows = await db.scalars(
            query_w.order_by(WorkRequest.updated_at.desc(), WorkRequest.id.desc()).limit(fetch)
        )
        for request in request_rows:
            drafts.extend(_work_request_cards(request))

    if _wants(_TASK_KINDS) and not empty_scope and not empty_agents:
        query_t = select(Task).where(
            Task.workspace_id == workspace_id, Task.assigned_agent_id.is_not(None)
        )
        if before is not None:
            query_t = query_t.where(Task.created_at < before)
        if scope is not None:
            query_t = query_t.where(Task.id.in_(scope))
        if agent_filter is not None:
            query_t = query_t.where(Task.assigned_agent_id.in_(list(agent_filter)))
        task_rows = await db.scalars(
            query_t.order_by(Task.updated_at.desc(), Task.id.desc()).limit(fetch)
        )
        for task in task_rows:
            drafts.extend(_task_cards(task))

    if _wants(frozenset({ActivityKind.NEEDS_REVIEW})) and not empty_scope and not empty_agents:
        query_a = select(Approval).where(
            Approval.workspace_id == workspace_id,
            Approval.status == ApprovalStatus.PENDING.value,
        )
        if before is not None:
            query_a = query_a.where(Approval.requested_at < before)
        if scope is not None:
            query_a = query_a.where(Approval.task_id.in_(scope))
        if agent_filter is not None:
            query_a = query_a.where(Approval.requested_by_agent_id.in_(list(agent_filter)))
        approval_rows = await db.scalars(
            query_a.order_by(Approval.requested_at.desc(), Approval.id.desc()).limit(fetch)
        )
        drafts.extend(_approval_card(a) for a in approval_rows)

        query_r = select(WorkReview).where(
            WorkReview.workspace_id == workspace_id,
            WorkReview.status == WorkReviewStatus.PENDING.value,
        )
        if before is not None:
            query_r = query_r.where(WorkReview.requested_at < before)
        if scope is not None:
            query_r = query_r.where(WorkReview.task_id.in_(scope))
        if agent_filter is not None:
            ids = list(agent_filter)
            query_r = query_r.where(
                or_(WorkReview.subject_agent_id.in_(ids), WorkReview.reviewer_agent_id.in_(ids))
            )
        review_rows = await db.scalars(
            query_r.order_by(WorkReview.requested_at.desc(), WorkReview.id.desc()).limit(fetch)
        )
        drafts.extend(_review_card(r) for r in review_rows)

    # Post-filters that the SQL predicates cannot express exactly.
    drafts = [
        d
        for d in drafts
        if (kinds is None or d.kind in kinds)
        and (before is None or _aware(d.created_at) < _aware(before))
        and (
            agent_filter is None
            or d.actor_agent_id in agent_filter
            or d.target_agent_id in agent_filter
        )
    ]
    drafts.sort(key=lambda d: (_aware(d.created_at), d.id), reverse=True)
    drafts = drafts[:limit]

    lineage = await _task_lineage(
        db, workspace_id, {d.task_id for d in drafts if d.task_id is not None}
    )
    names = await tasks_service.agent_names(
        db,
        workspace_id,
        list(
            {d.actor_agent_id for d in drafts if d.actor_agent_id}
            | {d.target_agent_id for d in drafts if d.target_agent_id}
        ),
    )
    failure_reasons = await _latest_run_errors(
        db,
        workspace_id,
        [d.task_id for d in drafts if d.kind is ActivityKind.FAILED and d.task_id is not None],
    )

    items: list[ActivityCardOut] = []
    for draft in drafts:
        card_task = lineage.get(draft.task_id) if draft.task_id is not None else None
        root = _root_of(draft.task_id, lineage) if draft.task_id is not None else None
        actor_name = names.get(draft.actor_agent_id) if draft.actor_agent_id else None
        target_name = draft.target_agent_name or (
            names.get(draft.target_agent_id) if draft.target_agent_id else None
        )
        summary = draft.summary
        detail_json = draft.detail_json or {}
        if not summary and card_task is not None:
            template = _TASK_SUMMARY_TEMPLATES.get(draft.kind, "{agent}: “{title}”.")
            summary = template.format(agent=actor_name or "An agent", title=card_task.title)
            reason = (
                failure_reasons.get(card_task.id) if draft.kind is ActivityKind.FAILED else None
            )
            if reason:
                # Plain-language reason (e.g. the provider's own message) so the
                # card explains what went wrong without opening Advanced.
                summary = f"{summary} {reason}"
                detail_json = {**detail_json, "error_message": reason}
            summary = _truncate(summary, SUMMARY_CHARS)
        elif not summary:
            summary = ACTIVITY_LABELS[draft.kind]
        items.append(
            ActivityCardOut(
                id=draft.id,
                kind=draft.kind,
                label=ACTIVITY_LABELS[draft.kind],
                actor_type=draft.actor_type,
                actor_agent_id=draft.actor_agent_id,
                actor_agent_name=actor_name,
                target_agent_id=draft.target_agent_id,
                target_agent_name=target_name,
                task_id=draft.task_id,
                task_title=card_task.title if card_task is not None else None,
                root_task_id=root.id if root is not None else None,
                conversation_id=(
                    (card_task.conversation_id if card_task is not None else None)
                    or (root.conversation_id if root is not None else None)
                ),
                approval_id=draft.approval_id,
                work_request_id=draft.work_request_id,
                review_id=draft.review_id,
                summary=summary,
                detail_json=detail_json,
                created_at=draft.created_at,
            )
        )
    next_before = items[-1].created_at if len(items) == limit else None
    return ActivityListOut(items=items, next_before=next_before)


# --- Attention ---


async def _recent_failures(db: AsyncSession, workspace_id: UUID) -> list[Task]:
    """Failed tasks from the last week that nobody has dismissed yet."""
    rows = await db.scalars(
        select(Task)
        .where(
            Task.workspace_id == workspace_id,
            Task.state == TaskState.FAILED.value,
            Task.updated_at >= _now() - FAILED_TASK_WINDOW,
        )
        .order_by(Task.updated_at.desc())
        .limit(MAX_PAGE_SIZE)
    )
    return [t for t in rows if not tasks_service.is_attention_acknowledged(t)]


async def acknowledge_failures(
    db: AsyncSession,
    ctx: WorkspaceContext,
    *,
    request_id: UUID,
    ip_hash: str,
) -> AcknowledgeFailuresOut:
    """Dismiss every failure the inbox currently lists, in one transaction."""
    stamped_at = _now()
    acknowledged: list[UUID] = []
    for task in await _recent_failures(db, ctx.workspace_id):
        if not tasks_service.mark_attention_acknowledged(task, at=stamped_at):
            continue
        acknowledged.append(task.id)
        audit.record(
            db,
            action="task.acknowledged",
            target_type="task",
            target_id=task.id,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user.id,
            request_id=request_id,
            ip_hash=ip_hash,
            metadata={"bulk": True},
        )
    if acknowledged:
        await db.commit()
    return AcknowledgeFailuresOut(acknowledged=len(acknowledged), task_ids=acknowledged)


async def attention(db: AsyncSession, workspace_id: UUID) -> AttentionOut:
    approvals = list(
        await db.scalars(
            select(Approval)
            .where(
                Approval.workspace_id == workspace_id,
                Approval.status == ApprovalStatus.PENDING.value,
            )
            .order_by(Approval.requested_at.desc())
            .limit(MAX_PAGE_SIZE)
        )
    )
    failed = await _recent_failures(db, workspace_id)
    active_conversations = list(
        await db.scalars(
            select(Conversation)
            .where(
                Conversation.workspace_id == workspace_id,
                Conversation.status == ConversationStatus.ACTIVE.value,
            )
            .order_by(Conversation.last_activity_at.desc())
            .limit(MAX_PAGE_SIZE)
        )
    )
    projected = await project_conversations(db, workspace_id, active_conversations)
    waiting = [c for c in projected if c.active_run_status == RunStatus.WAITING_APPROVAL.value]
    # Work reviews assigned to a human (including fail-closed mandatory
    # reviews with no resolvable AI reviewer) need a person now; the ones an
    # AI colleague is handling are listed so a person can step in.
    review_rows = list(
        await db.scalars(
            select(WorkReview)
            .where(
                WorkReview.workspace_id == workspace_id,
                WorkReview.status == WorkReviewStatus.PENDING.value,
                WorkReview.reviewer_type.in_((ReviewerType.HUMAN.value, ReviewerType.AGENT.value)),
            )
            .order_by(WorkReview.requested_at.desc())
            .limit(MAX_PAGE_SIZE)
        )
    )
    projected_reviews = await coordination.project_reviews(db, workspace_id, review_rows)
    pending_reviews = [r for r in projected_reviews if r.reviewer_type == ReviewerType.HUMAN.value]
    reviews_in_progress = [
        r for r in projected_reviews if r.reviewer_type == ReviewerType.AGENT.value
    ]
    counts = AttentionCounts(
        approvals=len(approvals),
        failures=len(failed),
        reviews=len(pending_reviews),
        reviews_in_progress=len(reviews_in_progress),
        total=len(approvals) + len(failed) + len(waiting) + len(pending_reviews),
    )
    return AttentionOut(
        pending_approvals=[ApprovalOut.model_validate(a) for a in approvals],
        failed_tasks=[TaskOut.model_validate(t) for t in failed],
        waiting_conversations=waiting,
        pending_reviews=pending_reviews,
        reviews_in_progress=reviews_in_progress,
        counts=counts,
    )
