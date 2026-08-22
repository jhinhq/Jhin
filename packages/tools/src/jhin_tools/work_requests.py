"""Peer and cross-team work requests: durable, idempotent asks for help.

Delegation (``organization.delegate_task``) transfers ownership of a child
task inside a lineage. A work request only *asks*: the target agent (or a
human on its behalf) accepts, declines, or asks for clarification. Accepting
creates exactly one standalone task — ``parent_task_id`` stays NULL and the
link lives in ``work_request.created_task_id`` plus task metadata
``{"origin": "work_request", ...}``.

Authorization for the requester runs through the gateway: the
``organization.work.request`` grant plus :func:`jhin_policy.evaluate_work_request`
(structural guards: self/inactive/unavailable target, depth, caps,
ping-pong). The responder tool is structurally limited to the request's
target agent. Humans reach the same service functions through the API.

Worker integration: when ``organization.respond_work_request`` returns
``created_task_id``, the agent worker lifts it into
``StepResult.work_request_starts`` and ``AgentTaskWorkflow`` starts one
durable ``WorkRequestTaskWorkflow`` (see ``docs/architecture/coordination.md``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AuditEvent, Message, Task, WorkRequest, Workspace
from jhin_domain import (
    WORK_REQUEST_ACTIVE_STATUSES,
    WORK_REQUEST_OPEN_STATUSES,
    ActorType,
    AgentStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    WorkRequestStatus,
    new_uuid7,
    structured_content,
)
from jhin_policy import (
    WORK_REQUEST_CAPABILITY,
    WORK_RESPOND_CAPABILITY,
    DecisionType,
    Grant,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
    WorkRequestFacts,
    coordination_settings,
    evaluate_work_request,
)
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor, ToolValidator
from jhin_tools.organization import _is_subordinate

_MAX_TASK_ANCESTORS = 50
_ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)

Decision = Literal["accept", "decline", "clarify"]


class WorkRequestError(Exception):
    """A rejected transition; ``code`` is stable and user-safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


# --- facts ---


async def root_task_id(
    session: AsyncSession, workspace_id: UUID, task_id: UUID | None
) -> UUID | None:
    """Top of the task's lineage across delegation parents and earlier
    work-request hops (bounded walk)."""
    current = task_id
    top: UUID | None = None
    for _ in range(_MAX_TASK_ANCESTORS):
        if current is None:
            break
        task = await session.scalar(
            select(Task).where(Task.id == current, Task.workspace_id == workspace_id)
        )
        if task is None:
            break
        top = task.id
        if task.parent_task_id is not None:
            current = task.parent_task_id
            continue
        origin = task.metadata_json.get("work_request")
        if isinstance(origin, dict):
            raw = origin.get("root_task_id")
            if isinstance(raw, str):
                try:
                    return UUID(raw)
                except ValueError:
                    return top
        break
    return top


def _request_depth_of(task: Task | None) -> int:
    if task is None:
        return 1
    origin = task.metadata_json.get("work_request")
    if isinstance(origin, dict) and isinstance(origin.get("depth"), int):
        return int(origin["depth"]) + 1
    return 1


async def load_work_request_facts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    requester_agent_id: UUID,
    target_agent_id: str,
    task_id: UUID | None,
) -> WorkRequestFacts:
    try:
        target_uuid = UUID(target_agent_id)
    except ValueError:
        return WorkRequestFacts(
            requester_agent_id=str(requester_agent_id), target_agent_id=target_agent_id
        )
    target = await session.scalar(
        select(Agent).where(Agent.id == target_uuid, Agent.workspace_id == workspace_id)
    )
    if target is None:
        return WorkRequestFacts(
            requester_agent_id=str(requester_agent_id), target_agent_id=str(target_uuid)
        )
    requester = await session.scalar(
        select(Agent).where(Agent.id == requester_agent_id, Agent.workspace_id == workspace_id)
    )
    task = None
    if task_id is not None:
        task = await session.scalar(
            select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
        )
    root = await root_task_id(session, workspace_id, task_id)
    same_team = (
        requester is not None
        and requester.team_id is not None
        and requester.team_id == target.team_id
    )
    open_count = await session.scalar(
        select(func.count())
        .select_from(WorkRequest)
        .where(
            WorkRequest.workspace_id == workspace_id,
            WorkRequest.requester_agent_id == requester_agent_id,
            WorkRequest.status.in_([s.value for s in WORK_REQUEST_OPEN_STATUSES]),
        )
    )
    hour_count = await session.scalar(
        select(func.count())
        .select_from(WorkRequest)
        .where(
            WorkRequest.workspace_id == workspace_id,
            WorkRequest.requester_agent_id == requester_agent_id,
            WorkRequest.created_at >= _now() - timedelta(hours=1),
        )
    )
    active_for_target = await session.scalar(
        select(func.count())
        .select_from(WorkRequest)
        .where(
            WorkRequest.workspace_id == workspace_id,
            WorkRequest.target_agent_id == target_uuid,
            WorkRequest.status == WorkRequestStatus.ACCEPTED.value,
        )
    )
    reverse = None
    if root is not None:
        reverse = await session.scalar(
            select(WorkRequest.id).where(
                WorkRequest.workspace_id == workspace_id,
                WorkRequest.requester_agent_id == target_uuid,
                WorkRequest.target_agent_id == requester_agent_id,
                WorkRequest.root_task_id == root,
                WorkRequest.status.in_([s.value for s in WORK_REQUEST_ACTIVE_STATUSES]),
            )
        )
    return WorkRequestFacts(
        requester_agent_id=str(requester_agent_id),
        target_agent_id=str(target_uuid),
        target_exists=True,
        target_active=target.status == AgentStatus.ACTIVE.value,
        target_available=target.availability == "available",
        target_is_subordinate=await _is_subordinate(
            session, workspace_id, requester_agent_id, target_uuid
        ),
        target_in_same_team=same_team,
        request_depth=_request_depth_of(task),
        open_requests_by_requester=int(open_count or 0),
        requests_last_hour_by_requester=int(hour_count or 0),
        active_request_tasks_for_target=int(active_for_target or 0),
        reverse_request_open=reverse is not None,
    )


# --- service ---


def _agent_message(
    *,
    workspace_id: UUID,
    task: Task | None,
    conversation_id: UUID | None,
    run_id: UUID | None,
    sender_id: UUID,
    recipient_id: UUID,
    message_type: MessageType,
    content: dict[str, Any],
) -> Message:
    return Message(
        id=new_uuid7(),
        workspace_id=workspace_id,
        task_id=task.id if task is not None else None,
        run_id=run_id,
        conversation_id=conversation_id,
        sender_type=SenderType.AGENT.value,
        sender_id=sender_id,
        recipient_type=RecipientType.AGENT.value,
        recipient_id=recipient_id,
        message_type=message_type.value,
        content_json=content,
        visibility=MessageVisibility.VISIBLE.value,
    )


async def get_work_request(
    session: AsyncSession, workspace_id: UUID, request_id: UUID
) -> WorkRequest | None:
    request: WorkRequest | None = await session.scalar(
        select(WorkRequest).where(
            WorkRequest.id == request_id, WorkRequest.workspace_id == workspace_id
        )
    )
    return request


async def create_work_request(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    requester: Agent,
    target: Agent,
    requester_task: Task | None,
    requester_run_id: UUID | None,
    title: str,
    description: str,
    expected_output: str = "",
    idempotency_key: str,
    requested_by_user_id: UUID | None = None,
) -> tuple[WorkRequest, bool]:
    """Persist one request (idempotent on ``(workspace, idempotency_key)``)
    plus the structured ``question`` message on the requester's task.

    Returns ``(request, created)``. Callers own authorization and commit.
    """
    existing = await session.scalar(
        select(WorkRequest).where(
            WorkRequest.workspace_id == workspace_id,
            WorkRequest.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False
    if requester.id == target.id:
        raise WorkRequestError("self_request", "an agent cannot request work from itself")

    root = await root_task_id(session, workspace_id, requester_task.id if requester_task else None)
    request = WorkRequest(
        id=new_uuid7(),
        workspace_id=workspace_id,
        conversation_id=requester_task.conversation_id if requester_task else None,
        requester_agent_id=requester.id,
        requester_task_id=requester_task.id if requester_task else None,
        requester_run_id=requester_run_id,
        root_task_id=root,
        requested_by_user_id=requested_by_user_id,
        target_agent_id=target.id,
        title=title[:500],
        description=description,
        expected_output=expected_output,
        status=WorkRequestStatus.PENDING.value,
        idempotency_key=idempotency_key[:200],
        depth=_request_depth_of(requester_task),
        metadata_json={
            "requester_agent_name": requester.name,
            "target_agent_name": target.name,
        },
    )
    session.add(request)
    session.add(
        _agent_message(
            workspace_id=workspace_id,
            task=requester_task,
            conversation_id=request.conversation_id,
            run_id=requester_run_id,
            sender_id=requester.id,
            recipient_id=target.id,
            message_type=MessageType.QUESTION,
            content=structured_content(
                title,
                recommended_next_action="await_response",
                kind="work_request",
                work_request_id=str(request.id),
                status=request.status,
                target_agent_id=str(target.id),
                target_agent_name=target.name,
                from_agent_id=str(requester.id),
                from_agent_name=requester.name,
                instructions=description[:2_000],
                expected_output=expected_output[:2_000],
            ),
        )
    )
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_type=(ActorType.USER if requested_by_user_id else ActorType.AGENT).value,
            actor_id=requested_by_user_id or requester.id,
            action="work_request.created",
            target_type="work_request",
            target_id=request.id,
            metadata_json={
                "requester_agent_id": str(requester.id),
                "target_agent_id": str(target.id),
                "requester_task_id": str(requester_task.id) if requester_task else None,
                "depth": request.depth,
                "title": request.title,
            },
        )
    )
    await session.flush()
    return request, True


async def _status_message(
    session: AsyncSession,
    request: WorkRequest,
    *,
    sender_id: UUID,
    recipient_id: UUID,
    summary: str,
    message_type: MessageType = MessageType.STATUS,
    extra: dict[str, Any] | None = None,
) -> Message:
    task = None
    if request.requester_task_id is not None:
        task = await session.get(Task, request.requester_task_id)
    names = request.metadata_json
    message = _agent_message(
        workspace_id=request.workspace_id,
        task=task,
        conversation_id=request.conversation_id,
        run_id=None,
        sender_id=sender_id,
        recipient_id=recipient_id,
        message_type=message_type,
        content=structured_content(
            summary,
            kind="work_request",
            work_request_id=str(request.id),
            status=request.status,
            target_agent_id=str(request.target_agent_id),
            target_agent_name=str(names.get("target_agent_name", "") or ""),
            from_agent_id=str(sender_id),
            from_agent_name=(
                str(names.get("target_agent_name", "") or "")
                if sender_id == request.target_agent_id
                else str(names.get("requester_agent_name", "") or "")
            ),
            **(extra or {}),
        ),
    )
    session.add(message)
    return message


def _audit_transition(
    session: AsyncSession,
    request: WorkRequest,
    action: str,
    *,
    actor_type: ActorType,
    actor_id: UUID | None,
    extra: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            workspace_id=request.workspace_id,
            actor_type=actor_type.value,
            actor_id=actor_id,
            action=action,
            target_type="work_request",
            target_id=request.id,
            metadata_json={"status": request.status, **(extra or {})},
        )
    )


async def accept_work_request(
    session: AsyncSession,
    request: WorkRequest,
    *,
    response: str = "",
    decided_by_user_id: UUID | None = None,
) -> tuple[WorkRequest, Task, bool]:
    """Accept: create exactly one linked standalone task.

    Idempotent — an already-accepted request returns its existing task with
    ``created=False``. Declined/terminal requests cannot be accepted.
    Callers own authorization (target agent structurally, or admin) and the
    workflow start (``WorkRequestTaskWorkflow``).
    """
    if request.status == WorkRequestStatus.ACCEPTED.value and request.created_task_id is not None:
        task = await session.get(Task, request.created_task_id)
        if task is not None:
            return request, task, False
    if request.status not in {s.value for s in WORK_REQUEST_OPEN_STATUSES}:
        raise WorkRequestError("work_request_not_open", f"work request is already {request.status}")
    target = await session.get(Agent, request.target_agent_id)
    if target is None or target.status != AgentStatus.ACTIVE.value:
        raise WorkRequestError("target_inactive", "the target agent is not active")
    requester_task = (
        await session.get(Task, request.requester_task_id) if request.requester_task_id else None
    )
    description = request.description
    if request.expected_output:
        description += f"\n\nExpected output: {request.expected_output}"
    task = Task(
        id=new_uuid7(),
        workspace_id=request.workspace_id,
        title=request.title[:500],
        description=description,
        state=TaskState.QUEUED.value,
        priority=requester_task.priority if requester_task else "normal",
        assigned_agent_id=target.id,
        conversation_id=request.conversation_id,
        correlation_id=requester_task.correlation_id if requester_task else new_uuid7(),
        metadata_json={
            "origin": "work_request",
            "work_request": {
                "id": str(request.id),
                "requester_agent_id": str(request.requester_agent_id),
                "requester_agent_name": request.metadata_json.get("requester_agent_name", ""),
                "requester_task_id": (
                    str(request.requester_task_id) if request.requester_task_id else ""
                ),
                "root_task_id": str(request.root_task_id) if request.root_task_id else "",
                "depth": request.depth,
                "expected_output": request.expected_output,
            },
        },
    )
    task.temporal_workflow_id = f"task-{task.id}"
    session.add(task)
    request.status = WorkRequestStatus.ACCEPTED.value
    request.created_task_id = task.id
    request.response = response[:4_000]
    request.responded_at = _now()
    await _status_message(
        session,
        request,
        sender_id=request.target_agent_id,
        recipient_id=request.requester_agent_id,
        summary=response or f"Accepted: {request.title}",
        extra={"created_task_id": str(task.id)},
    )
    _audit_transition(
        session,
        request,
        "work_request.accepted",
        actor_type=ActorType.USER if decided_by_user_id else ActorType.AGENT,
        actor_id=decided_by_user_id or request.target_agent_id,
        extra={"created_task_id": str(task.id)},
    )
    await session.flush()
    return request, task, True


async def decline_work_request(
    session: AsyncSession,
    request: WorkRequest,
    *,
    response: str = "",
    decided_by_user_id: UUID | None = None,
) -> WorkRequest:
    """Decline: no task is ever created. Idempotent on repeat."""
    if request.status == WorkRequestStatus.DECLINED.value:
        return request
    if request.status not in {s.value for s in WORK_REQUEST_OPEN_STATUSES}:
        raise WorkRequestError("work_request_not_open", f"work request is already {request.status}")
    request.status = WorkRequestStatus.DECLINED.value
    request.response = response[:4_000]
    request.responded_at = _now()
    request.completed_at = request.responded_at
    await _status_message(
        session,
        request,
        sender_id=request.target_agent_id,
        recipient_id=request.requester_agent_id,
        summary=response or f"Declined: {request.title}",
    )
    _audit_transition(
        session,
        request,
        "work_request.declined",
        actor_type=ActorType.USER if decided_by_user_id else ActorType.AGENT,
        actor_id=decided_by_user_id or request.target_agent_id,
    )
    await session.flush()
    return request


async def request_clarification(
    session: AsyncSession,
    request: WorkRequest,
    *,
    response: str,
    decided_by_user_id: UUID | None = None,
) -> WorkRequest:
    if request.status not in {s.value for s in WORK_REQUEST_OPEN_STATUSES}:
        raise WorkRequestError("work_request_not_open", f"work request is already {request.status}")
    request.status = WorkRequestStatus.CLARIFICATION_REQUESTED.value
    request.response = response[:4_000]
    request.responded_at = _now()
    await _status_message(
        session,
        request,
        sender_id=request.target_agent_id,
        recipient_id=request.requester_agent_id,
        summary=response,
        message_type=MessageType.QUESTION,
    )
    _audit_transition(
        session,
        request,
        "work_request.clarification_requested",
        actor_type=ActorType.USER if decided_by_user_id else ActorType.AGENT,
        actor_id=decided_by_user_id or request.target_agent_id,
    )
    await session.flush()
    return request


async def finalize_work_request(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    request_id: UUID,
    run_status: str,
) -> WorkRequest | None:
    """Terminal projection after the created task's workflow ends: mark the
    request completed/failed and post the standardized ``result`` message on
    the requester's task (summary, artifacts, risks — never a transcript).
    Idempotent: a second call returns the already-terminal request."""
    request = await get_work_request(session, workspace_id, request_id)
    if request is None:
        return None
    if request.status in (WorkRequestStatus.COMPLETED.value, WorkRequestStatus.FAILED.value):
        return request
    task = await session.get(Task, request.created_task_id) if request.created_task_id else None
    reported = task.metadata_json.get("reported_result") if task is not None else None
    reported = reported if isinstance(reported, dict) else {}
    status_value = str(reported.get("status", "") or "")
    completed = run_status == "completed" and status_value not in ("fail", "blocked")
    request.status = (
        WorkRequestStatus.COMPLETED.value if completed else WorkRequestStatus.FAILED.value
    )
    request.completed_at = _now()
    summary = str(reported.get("summary", "") or "")
    if not summary:
        summary = (
            f"Finished: {request.title}"
            if completed
            else f"Could not complete: {request.title} ({run_status})"
        )
    await _status_message(
        session,
        request,
        sender_id=request.target_agent_id,
        recipient_id=request.requester_agent_id,
        summary=summary,
        message_type=MessageType.RESULT,
        extra={
            "artifacts": reported.get("artifacts", []),
            "risks": reported.get("risks", []),
            "recommended_next_action": reported.get("recommended_next_action", ""),
            "created_task_id": str(request.created_task_id) if request.created_task_id else "",
            "run_status": run_status,
        },
    )
    _audit_transition(
        session,
        request,
        "work_request.completed" if completed else "work_request.failed",
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        extra={"run_status": run_status},
    )
    await session.flush()
    return request


# --- gateway tools ---


class RequestWorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20_000)
    expected_output: str = Field(default="", max_length=4_000)
    # Retries with the same key never create a second request.
    idempotency_key: str = Field(default="", max_length=200)


class RequestWorkOutput(BaseModel):
    work_request_id: str
    status: str
    target_agent_id: str
    target_agent_name: str
    created: bool
    detail: str = ""


class RespondWorkRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_request_id: str = Field(min_length=1, max_length=64)
    decision: Decision
    response: str = Field(default="", max_length=4_000)


class RespondWorkRequestOutput(BaseModel):
    work_request_id: str
    status: str
    created_task_id: str | None = None
    detail: str = ""


def default_idempotency_key(
    run_id: UUID, target_agent_id: str, title: str, description: str
) -> str:
    digest = hashlib.sha256(f"{target_agent_id}\n{title}\n{description}".encode()).hexdigest()
    return f"run:{run_id}:{digest[:32]}"


async def validate_request_work(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    data = cast(RequestWorkInput, payload)
    facts = await load_work_request_facts(
        ctx.session,
        workspace_id=ctx.workspace_id,
        requester_agent_id=ctx.agent_id,
        target_agent_id=data.target_agent_id,
        task_id=ctx.task_id,
    )
    workspace = await ctx.session.get(Workspace, ctx.workspace_id)
    settings = coordination_settings(workspace.settings_json if workspace is not None else None)
    decision = evaluate_work_request(grants, facts, settings)
    if decision.allowed:
        return None
    return PolicyDecision(decision=DecisionType.DENY, code=decision.code, reason=decision.reason)


async def _request_work(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RequestWorkInput, payload)
    target = await ctx.session.scalar(
        select(Agent).where(
            Agent.id == UUID(data.target_agent_id), Agent.workspace_id == ctx.workspace_id
        )
    )
    requester = await ctx.session.scalar(
        select(Agent).where(Agent.id == ctx.agent_id, Agent.workspace_id == ctx.workspace_id)
    )
    if target is None or requester is None:
        raise ValueError("work request participants disappeared before execution")
    task = await ctx.session.scalar(
        select(Task).where(Task.id == ctx.task_id, Task.workspace_id == ctx.workspace_id)
    )
    key = data.idempotency_key or default_idempotency_key(
        ctx.run_id, data.target_agent_id, data.title, data.description
    )
    request, created = await create_work_request(
        ctx.session,
        workspace_id=ctx.workspace_id,
        requester=requester,
        target=target,
        requester_task=task,
        requester_run_id=ctx.run_id,
        title=data.title,
        description=data.description,
        expected_output=data.expected_output,
        idempotency_key=key,
    )
    return RequestWorkOutput(
        work_request_id=str(request.id),
        status=request.status,
        target_agent_id=str(target.id),
        target_agent_name=target.name,
        created=created,
        detail=(
            "request sent; the target decides whether to accept and the result "
            "arrives as a message when their task finishes"
            if created
            else "an identical request already exists; returning it"
        ),
    )


async def validate_respond_work_request(
    ctx: ToolExecutionContext, payload: BaseModel, grants: Sequence[Grant]
) -> PolicyDecision | None:
    """Structural: only the request's target agent may respond."""
    data = cast(RespondWorkRequestInput, payload)
    try:
        request_id = UUID(data.work_request_id)
    except ValueError:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="work_request_not_found",
            reason="no such work request in this workspace",
        )
    request = await get_work_request(ctx.session, ctx.workspace_id, request_id)
    if request is None:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="work_request_not_found",
            reason="no such work request in this workspace",
        )
    if request.target_agent_id != ctx.agent_id:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="not_request_target",
            reason="only the agent the request is addressed to may respond",
        )
    return None


async def _respond_work_request(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(RespondWorkRequestInput, payload)
    request = await get_work_request(ctx.session, ctx.workspace_id, UUID(data.work_request_id))
    if request is None:
        raise ValueError("work request disappeared before execution")
    try:
        if data.decision == "accept":
            request, task, created = await accept_work_request(
                ctx.session, request, response=data.response
            )
            return RespondWorkRequestOutput(
                work_request_id=str(request.id),
                status=request.status,
                created_task_id=str(task.id),
                detail=(
                    "accepted; a new task was created for you and starts after this step"
                    if created
                    else "already accepted; returning the existing task"
                ),
            )
        if data.decision == "decline":
            request = await decline_work_request(ctx.session, request, response=data.response)
            return RespondWorkRequestOutput(
                work_request_id=str(request.id), status=request.status, detail="declined"
            )
        request = await request_clarification(
            ctx.session, request, response=data.response or "Please clarify the request."
        )
    except WorkRequestError as exc:
        return RespondWorkRequestOutput(
            work_request_id=str(request.id), status=request.status, detail=f"{exc.code}: {exc}"
        )
    return RespondWorkRequestOutput(
        work_request_id=str(request.id),
        status=request.status,
        detail="clarification requested from the requester",
    )


WORK_REQUEST_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor, ToolValidator | None], ...] = (
    (
        ToolDefinition(
            name="organization.request_work",
            description=(
                "Ask another agent (a peer or someone on another team) for "
                "help with a piece of work. Unlike delegation, the target "
                "decides whether to accept; if they do, a separate task is "
                "created for them and the result arrives as a message. Use "
                "organization.directory.search to find the right colleague."
            ),
            risk=RiskLevel.WRITE,
            input_model=RequestWorkInput,
            output_model=RequestWorkOutput,
            required_capability=WORK_REQUEST_CAPABILITY,
            supports_approval=True,
            defers_scope=True,
        ),
        _request_work,
        validate_request_work,
    ),
    (
        ToolDefinition(
            name="organization.respond_work_request",
            description=(
                "Respond to a work request addressed to you: accept (creates "
                "a task for you), decline, or ask for clarification."
            ),
            risk=RiskLevel.WRITE,
            input_model=RespondWorkRequestInput,
            output_model=RespondWorkRequestOutput,
            required_capability=WORK_RESPOND_CAPABILITY,
            supports_approval=True,
        ),
        _respond_work_request,
        validate_respond_work_request,
    ),
)
