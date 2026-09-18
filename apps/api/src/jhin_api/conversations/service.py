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
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
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
    BudgetNoticeOut,
    ConversationAgentOut,
    ConversationDetailOut,
    ConversationMessageOut,
    ConversationOut,
    ConversationResumeOut,
    ConversationToolCallListOut,
    ConversationToolCallOut,
    FailureNoticeOut,
)
from jhin_api.coordination import service as coordination
from jhin_api.deps import WorkspaceContext
from jhin_api.personas import service as personas_service
from jhin_api.personas.schemas import AgentPersonaSummary
from jhin_api.public_payloads import public_tool_payload
from jhin_api.tasks import service as tasks_service
from jhin_api.tasks.schemas import TaskOut
from jhin_api.tasks.tool_call_projection import project_tool_calls
from jhin_connectors import build_default_definition_catalog
from jhin_db.budget import month_spend_micros, workspace_budget_settings
from jhin_db.models import (
    Agent,
    AgentRun,
    AgentTeamMembership,
    Approval,
    AuditEvent,
    Conversation,
    Message,
    Task,
    ToolCall,
    User,
    UserQuestion,
    WorkRequest,
    WorkReview,
    Workspace,
)
from jhin_db.models.editorial import EditorialAssignment, GhostEditorialReview
from jhin_domain import (
    ACTIVITY_LABELS,
    AGENT_MESSAGE_TYPES,
    RUN_ACTIVE_STATUSES,
    UNRECONCILED_TOOL_STATUSES,
    WORK_REQUEST_ACTIVE_STATUSES,
    ActivityKind,
    AgentStatus,
    ApprovalStatus,
    ConversationStatus,
    FailureNotice,
    MessageType,
    MessageVisibility,
    RecipientType,
    ReviewerType,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
    UserQuestionStatus,
    Wait,
    WorkingTime,
    WorkRequestStatus,
    WorkReviewStatus,
    WorkspaceRole,
    activity_phrase,
    failure_notice,
    new_uuid7,
    role_satisfies,
    waiting_for_colleague_phrase,
    working_time,
)
from jhin_events import EventEnvelope, EventPublisher, EventSource
from jhin_observability import get_logger, normalize_event_family
from jhin_secrets import SecretCrypto
from jhin_secrets.intake import capture_input, merge_capture_metadata, safe_input_text, secret_spans
from jhin_secrets.variables import VariableError

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


@dataclass(frozen=True)
class ResumeResult:
    conversation: Conversation
    #: The work episode now carrying the turn.
    task: Task
    #: The failed turn it took over from.
    resumed_task: Task
    #: False when an earlier press already started this one.
    created: bool


# ``metadata_json`` keys that tie a resumed turn to the failure it picks up.
# The forward link is the idempotency handle: it is written in the same
# transaction as the new task, so a second press finds the successor instead
# of starting a parallel one.
RESUMED_BY_KEY = "resumed_by_task_id"
RESUME_OF_KEY = "resume_of_task_id"

# Task ``origin`` values that mean "this task is a person's turn in the chat".
# Mirrors jhin_agent_worker.reasoning._CONVERSATION_ORIGINS and
# jhin_tools.builtin._CONVERSATION_ORIGINS: a colleague's task carries the
# requester's ``conversation_id`` so its answer lands in the thread, which is
# not the same as being a turn somebody typed — and only a typed turn is a
# thing to offer back.
_CONVERSATION_ORIGINS = frozenset({"conversation", "message"})


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
    """Current work first, then the newest waiting turn when nothing runs."""
    tasks = [
        task
        for task in tasks
        if task.parent_task_id is None and task.metadata_json.get("origin") != "work_request"
    ]
    return next((t for t in tasks if t.state in ("running", "paused")), None) or next(
        (t for t in tasks if t.state == "queued"), None
    )


# --- Carrying an editorial assignment into the next work episode ---

# Phases a later turn may still be working on. The list is an allow-list so an
# unrecognised phase -- a newer one this version does not know -- never reopens
# an assignment by accident.
_CONTINUABLE_ASSIGNMENT_PHASES = frozenset(
    {"brief", "draft", "blocked", "awaiting_review", "changes_requested"}
)
# Once a review reaches one of these the article's fate is the director's, not
# the next thing somebody types into the chat.
_SETTLED_REVIEW_STATUSES = frozenset({"approved", "publishing", "published", "uncertain"})


def _predecessor_turn(tasks: list[Task]) -> Task | None:
    """The newest episode this conversation ran, whatever state it ended in."""
    return next(
        (
            task
            for task in tasks
            if task.parent_task_id is None and task.metadata_json.get("origin") != "work_request"
        ),
        None,
    )


async def _continued_assignment_id(
    db: AsyncSession,
    workspace_id: UUID,
    conversation: Conversation,
    agent: Agent,
    predecessor: Task | None,
) -> str | None:
    """The editorial assignment a successor episode is still working on.

    A second turn about the same article is the same assignment, so its research
    and image receipts have to bind to it -- without this the evidence check in
    the Ghost connector rejects everything a later turn retrieved.

    The link is re-derived from the assignment row every time instead of being
    copied forward from task metadata. A task therefore cannot talk its way into
    someone else's editorial work by carrying the key, and a predecessor that
    disagrees with the row is treated as untrustworthy rather than merged.
    """
    if predecessor is None:
        return None
    if predecessor.metadata_json.get("stop_requested_at"):
        return None  # the person stopped this work; do not quietly resume it
    rows = list(
        await db.scalars(
            select(EditorialAssignment).where(
                EditorialAssignment.workspace_id == workspace_id,
                EditorialAssignment.conversation_id == conversation.id,
                EditorialAssignment.phase.in_(_CONTINUABLE_ASSIGNMENT_PHASES),
            )
        )
    )
    if len(rows) != 1:
        return None  # nothing open, or ambiguous -- the agent can say which one
    assignment = rows[0]
    if agent.id != assignment.writer_agent_id:
        return None
    if predecessor.assigned_agent_id != assignment.writer_agent_id:
        return None
    declared = {
        predecessor.metadata_json.get("editorial_assignment_id"),
        predecessor.metadata_json.get("work_request", {}).get("editorial_assignment_id"),
    } - {None}
    if declared - {str(assignment.id)}:
        return None  # the predecessor names a different assignment
    if predecessor.id != assignment.task_id and str(assignment.id) not in declared:
        return None  # no trusted chain back to the episode that opened it
    settled = await db.scalar(
        select(GhostEditorialReview.id).where(
            GhostEditorialReview.workspace_id == workspace_id,
            GhostEditorialReview.assignment_id == assignment.id,
            GhostEditorialReview.status.in_(_SETTLED_REVIEW_STATUSES),
        )
    )
    return None if settled else str(assignment.id)


# --- Picking a failed turn back up ---


def _is_turn_task(task: Task) -> bool:
    """Whether this task is a turn somebody typed into the chat."""
    return task.parent_task_id is None and task.metadata_json.get("origin") in _CONVERSATION_ORIGINS


def _resumable_task(tasks: list[Task]) -> Task | None:
    """The failed turn to offer back, or None.

    Only the *newest* turn is ever offered, and only when it failed. A failure
    further up the thread has been overtaken — the person asked something else
    afterwards and got an answer — and re-running it now would be the agent
    replying to a question two exchanges old.
    """
    newest = next((task for task in tasks if _is_turn_task(task)), None)
    if newest is None or newest.state != TaskState.FAILED.value:
        return None
    return newest


async def _latest_run(db: AsyncSession, workspace_id: UUID, task_id: UUID) -> AgentRun | None:
    run: AgentRun | None = await db.scalar(
        select(AgentRun)
        .where(AgentRun.workspace_id == workspace_id, AgentRun.task_id == task_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    return run


@lru_cache(maxsize=1)
def _repeatable_tool_names() -> frozenset[str]:
    """Tools whose own definition says running the call again is safe.

    Read straight off ``ToolDefinition.redispatch_is_safe`` — the declaration
    the tool layer already makes and recovery already acts on — rather than
    restated here as a list of names this module maintains. There is one
    answer to "may this be repeated?", it is written next to the tool's scope
    and risk, and this is that answer being read.

    A name the catalog does not know answers *no* by omission, which is the
    same conservative default the gateway takes: recovery never guesses, and
    neither does the offer a person is shown.
    """
    return frozenset(
        definition.name
        for definition in build_default_definition_catalog().definitions()
        if definition.redispatch_is_safe
    )


@dataclass(frozen=True, slots=True)
class _UnreconciledCalls:
    """The calls on a failed turn that nobody can account for, split by whether
    running them again is safe.

    Both halves are worth having. ``blocking`` decides whether the turn may be
    offered back at all; ``repeatable`` is why pressing the button is safe
    when it is offered, and a card that raises the doubt ("no record of
    whether it finished") and then never answers it leaves a person hovering
    over a control that is fine.
    """

    #: Newest call whose tool does not declare a repeat safe: (id, tool name).
    blocking: tuple[UUID, str] | None
    #: Newest call that does, when there is one.
    repeatable: tuple[UUID, str] | None


async def _unreconciled_calls(
    db: AsyncSession, workspace_id: UUID, task_id: UUID
) -> _UnreconciledCalls:
    """The calls on this task that nobody can account for, if any.

    Two things have to be true for a call to stop a turn being offered back,
    and both are somebody else's conclusion read from this end rather than a
    classification invented here:

    * the row is in :data:`jhin_domain.UNRECONCILED_TOOL_STATUSES` — its
      executor was entered and nothing can say what it then did; and
    * its tool does not declare a repeat safe.

    The status alone is not enough. It is a sound proxy only for rows the
    current recovery path wrote, because that path re-dispatches a
    ``redispatch_is_safe`` call and ends it terminal. A row from before the
    field existed, or one whose worker died before anything reconciled it,
    sits in the set regardless — which is exactly the incident this change
    exists for: ``cli.repository.checkout`` left ``execution_unknown`` by a
    redeploy, every byte of it a read, and a chat refusing to try again
    because "it may already have gone through".

    The split is done here rather than in SQL because both answers come from
    one pass: the rows are the unaccounted-for calls of a single failed turn,
    which is a handful at the very most.
    """
    rows = (
        await db.execute(
            select(ToolCall.id, ToolCall.tool_name)
            .join(AgentRun, AgentRun.id == ToolCall.run_id)
            .where(
                ToolCall.workspace_id == workspace_id,
                AgentRun.workspace_id == workspace_id,
                AgentRun.task_id == task_id,
                ToolCall.status.in_(sorted(s.value for s in UNRECONCILED_TOOL_STATUSES)),
            )
            .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
        )
    ).all()
    repeatable = _repeatable_tool_names()
    return _UnreconciledCalls(
        blocking=next(((r[0], r[1]) for r in rows if r[1] not in repeatable), None),
        repeatable=next(((r[0], r[1]) for r in rows if r[1] in repeatable), None),
    )


def _unreconciled_reason(tool_name: str, agent_name: str) -> str:
    """Why a turn cannot simply be run again, said to the person waiting.

    The tool reaches this sentence as a *phrase* and never as its own name,
    for the reason :mod:`jhin_domain.activity` gives: a denied row keeps
    whatever name the model asked for, and a name a model invented must not
    become a sentence on somebody's screen.
    """
    phrase = activity_phrase(tool_name)
    doing = f" that was {phrase[0].lower()}{phrase[1:]}" if phrase else ""
    who = agent_name or "your agent"
    return (
        f"One step{doing} never reported back, so it may already have gone through. "
        f"Trying again could repeat it. Check how it turned out, then tell {who} "
        "what to do next."
    )


def _ready_reason(agent_name: str, repeatable_tool: str | None) -> str:
    """Why pressing is safe, said before the press rather than assumed.

    The failure this card most often sits under says a step "was cut short
    before it could report back, so there is no record of whether it
    finished". That is a doubt, and it is a real one — it is why the *other*
    branch of this function refuses to offer the button at all. When the
    button is offered anyway, the reason is never that the doubt was
    imaginary: it is that the step in doubt declares a repeat safe, because
    everything it touches is Jhin's own workspace or somebody else's system
    read and not written (``ToolDefinition.redispatch_is_safe``). A card that
    raises the doubt and then goes quiet leaves a careful person hovering over
    a control that is fine, so the answer is said out loud.

    The tool is named as a phrase and never by its own name, for the reason
    :mod:`jhin_domain.activity` gives.
    """
    who = agent_name or "Your agent"
    picks_up = f"{who} can pick this up from your message — there's nothing to retype."
    if repeatable_tool is None:
        return picks_up
    phrase = activity_phrase(repeatable_tool)
    doing = f" was {phrase[0].lower()}{phrase[1:]}, which is" if phrase else " is"
    return (
        f"The step that didn't report back{doing} safe to run again, so nothing "
        f"can happen twice. {picks_up}"
    )


async def _resume_offer(
    db: AsyncSession,
    workspace_id: UUID,
    conversation: Conversation,
    tasks: list[Task],
    agent: Agent | None,
) -> ConversationResumeOut | None:
    """What a person can do about a chat whose last turn failed.

    ``None`` means there is nothing to offer — no failed turn, or something is
    already running — which is a client's signal to show no control at all
    rather than a dead one.
    """
    if _active_task(tasks) is not None:
        return None
    failed = _resumable_task(tasks)
    if failed is None or not failed.description.strip():
        # Nothing to send again is not a control with a sad face on it, it is
        # no control. (``resume_conversation`` still falls back to the seed
        # message for an API client; the button is only offered where the
        # words are already in hand.)
        return None
    run = await _latest_run(db, workspace_id, failed.id)
    common: dict[str, Any] = {
        "task_id": failed.id,
        "run_id": run.id if run is not None else None,
        "instruction": failed.description,
    }
    name = agent.name if agent is not None else ""

    unreconciled = await _unreconciled_calls(db, workspace_id, failed.id)
    if unreconciled.blocking is not None:
        call_id, tool_name = unreconciled.blocking
        return ConversationResumeOut(
            **common,
            state="blocked",
            reason=_unreconciled_reason(tool_name, name),
            unreconciled_tool_call_id=call_id,
        )

    if conversation.status != ConversationStatus.ACTIVE.value:
        return ConversationResumeOut(
            **common,
            state="unavailable",
            reason="This chat is archived. Restore it to pick this back up.",
        )
    if agent is None:
        return ConversationResumeOut(
            **common,
            state="unavailable",
            reason="This agent is no longer in the workspace.",
        )
    if agent.status != AgentStatus.ACTIVE.value:
        how = "paused by an admin" if agent.status == AgentStatus.PAUSED.value else "turned off"
        return ConversationResumeOut(
            **common,
            state="unavailable",
            reason=f"{name} is {how}, so this can't be picked up right now.",
        )
    return ConversationResumeOut(
        **common,
        state="ready",
        reason=_ready_reason(
            name,
            unreconciled.repeatable[1] if unreconciled.repeatable is not None else None,
        ),
    )


# A call whose name the registry recognized, or one still in flight. A
# ``denied`` row persists whatever name the model asked for, and a name the
# model invented must never become a sentence on somebody's screen; ``failed``
# and ``execution_unknown`` are not what the agent is doing *now*.
_ACTIVITY_TOOL_STATUSES = (
    ToolCallStatus.PENDING_APPROVAL.value,
    ToolCallStatus.PENDING_REVIEW.value,
    # Both halves of the durable claim: ``claimed`` is the moment between
    # owning the call and dispatching it, and the agent is just as much
    # "doing that thing" there as it is a statement later.
    ToolCallStatus.CLAIMED.value,
    ToolCallStatus.EXECUTING.value,
    ToolCallStatus.COMPLETED.value,
)
# How long a finished tool call still describes what the agent is doing. A
# model call between steps takes seconds, so a call older than this is a step
# the agent has already moved on from; saying otherwise is a small, constant
# lie on the one surface whose whole job is to say what is happening now. A
# call parked on an approval is exempt -- it can wait for a person for hours
# and is still exactly what the run is doing.
ACTIVITY_FRESH_FOR = timedelta(seconds=90)
_ACTIVITY_PARKED_STATUSES = (
    ToolCallStatus.PENDING_APPROVAL.value,
    ToolCallStatus.PENDING_REVIEW.value,
)


async def _active_activity(db: AsyncSession, workspace_id: UUID, task: Task) -> str | None:
    """What the agent on this task is doing right now, as a whole sentence.

    Derived from the newest tool call on a still-live run of this task, and
    from the tool's *name* only — see :mod:`jhin_domain.activity` for why no
    argument is passed in. ``None`` between steps, or for a tool whose name
    would tell a person nothing; the caller then shows the generic "Working".

    One query (a second only for the colleague's name), and only on the
    conversation *detail*: the chat list polls every row it shows, and a
    query per row there is a poll that gets more expensive the more
    conversations a workspace has.
    """
    newest = (
        await db.execute(
            select(ToolCall.tool_name, ToolCall.run_id)
            .join(AgentRun, AgentRun.id == ToolCall.run_id)
            .where(
                ToolCall.workspace_id == workspace_id,
                AgentRun.workspace_id == workspace_id,
                AgentRun.task_id == task.id,
                AgentRun.status.in_([s.value for s in RUN_ACTIVE_STATUSES]),
                ToolCall.status.in_(_ACTIVITY_TOOL_STATUSES),
                # Recent, or it is not what the agent is doing *now*. Without
                # this the newest call is announced in the present tense for as
                # long as the run lives -- "Saving this to memory" while the
                # agent has moved on to writing the answer, and a step that
                # finished hours ago still described as current. A step that
                # outlives the window is honestly unknown, and the caller falls
                # back to the generic "Working".
                or_(
                    ToolCall.status.in_(_ACTIVITY_PARKED_STATUSES),
                    ToolCall.created_at >= _now() - ACTIVITY_FRESH_FOR,
                ),
            )
            .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
            .limit(1)
        )
    ).first()
    if newest is None:
        return None
    tool_name, run_id = newest
    phrase = activity_phrase(tool_name)
    if tool_name != "organization.request_work":
        return phrase
    # A colleague's name is worth the extra query: parked on an answer, this
    # thread otherwise shows "Working…" for up to two minutes. The name comes
    # from the work_request row the platform wrote, never from the tool's
    # arguments.
    colleague = await db.scalar(
        select(WorkRequest.metadata_json)
        .where(
            WorkRequest.workspace_id == workspace_id,
            WorkRequest.requester_run_id == run_id,
            WorkRequest.status.in_([s.value for s in WORK_REQUEST_ACTIVE_STATUSES]),
        )
        .order_by(WorkRequest.created_at.desc())
        .limit(1)
    )
    if colleague is None:
        return phrase
    return waiting_for_colleague_phrase(str(colleague.get("target_agent_name", "") or ""))


# --- Projection ---


async def _run_waits(
    db: AsyncSession, workspace_id: UUID, run_ids: list[UUID]
) -> dict[UUID, list[Wait]]:
    """Every span these runs spent parked on somebody, from the rows that are
    the waits.

    Three tables, because a run can be stopped by three different people: an
    approval (a person decides), a question (a person answers), a work review
    (a person or a reviewing agent rules). Each already records when it opened
    and when it closed, to the millisecond, and each is the authority on its
    own wait — which is why this is read rather than accumulated into a column
    a crashed worker could fail to write.

    A row still ``pending`` has no end: it is open, and
    :func:`jhin_domain.working_time` treats that as "parked right now". A row
    that ended in any other way but never stamped its decision time (a
    cancelled approval, an expired question) falls back to ``updated_at``,
    which is when it stopped being a wait.

    **A blocking delegation is deliberately not a fourth table.** A run in
    ``waiting_delegation`` is parked, but it is parked on a colleague doing
    this same reply, not on somebody's decision about it — and the number
    these waits are subtracted from is what the product calls "how long this
    reply has been working, not counting time it spent waiting on *you*". A
    colleague's stretch is work on the reply, so it stays in the total; only
    the reader's own deliberation comes out. What must not happen is the chat
    calling that stretch *this* agent's thinking while it runs, and that is
    handled where it belongs, on the surface: ``statusLabelFor``
    (``apps/web/lib/chat.ts``) gives ``waiting_delegation`` its own label and
    shows no ticking clock, exactly as it does for the three waits above.
    """
    waits: dict[UUID, list[Wait]] = {}
    if not run_ids:
        return waits

    def add(run_id: UUID | None, started: datetime | None, ended: datetime | None) -> None:
        if run_id is None or started is None:
            return
        waits.setdefault(run_id, []).append(Wait(started_at=started, ended_at=ended))

    approvals = await db.execute(
        select(
            Approval.run_id,
            Approval.requested_at,
            Approval.decided_at,
            Approval.updated_at,
            Approval.status,
        ).where(Approval.workspace_id == workspace_id, Approval.run_id.in_(run_ids))
    )
    for run_id, requested_at, decided_at, updated_at, status_value in approvals.all():
        open_still = status_value == ApprovalStatus.PENDING.value
        add(run_id, requested_at, None if open_still else (decided_at or updated_at))

    questions = await db.execute(
        select(
            UserQuestion.run_id,
            UserQuestion.asked_at,
            UserQuestion.answered_at,
            UserQuestion.updated_at,
            UserQuestion.status,
        ).where(UserQuestion.workspace_id == workspace_id, UserQuestion.run_id.in_(run_ids))
    )
    for run_id, asked_at, answered_at, updated_at, status_value in questions.all():
        open_still = status_value == UserQuestionStatus.PENDING.value
        add(run_id, asked_at, None if open_still else (answered_at or updated_at))

    reviews = await db.execute(
        select(
            WorkReview.run_id,
            WorkReview.requested_at,
            WorkReview.decided_at,
            WorkReview.updated_at,
            WorkReview.status,
        ).where(WorkReview.workspace_id == workspace_id, WorkReview.run_id.in_(run_ids))
    )
    for run_id, requested_at, decided_at, updated_at, status_value in reviews.all():
        open_still = status_value == WorkReviewStatus.PENDING.value
        add(run_id, requested_at, None if open_still else (decided_at or updated_at))

    return waits


async def _working_times(
    db: AsyncSession, workspace_id: UUID, runs: list[tasks_service.LatestRun]
) -> dict[UUID, WorkingTime]:
    """How long each of these runs has actually been thinking.

    ``started_at`` alone answers "how long since this turn began", which is
    the number a person reads as "how long the agent has been thinking" and is
    not that at all: a run waiting overnight on an approval keeps its original
    stamp and re-enters ``running`` with it, so the morning shows hours of
    thought that never happened. The waits come out here rather than being
    re-stamped onto the run, so ``started_at`` keeps meaning what the metrics
    and the audit already read it to mean, and every run already in the
    database gets the right number rather than only the ones from here on.
    """
    started = [run for run in runs if run.started_at is not None]
    if not started:
        return {}
    waits = await _run_waits(db, workspace_id, [run.id for run in started])
    # ``completed_at`` bounds the answer. A wait row names a run without being
    # bounded by it, and this workspace's database holds an approval requested
    # 2h19m after the run it names had finished — without the bound that row
    # reported 8361 seconds of thinking for a run that lived 58.
    return {
        run.id: working_time(run.started_at, waits.get(run.id, ()), ended_at=run.completed_at)
        for run in started
    }


async def project_conversations(
    db: AsyncSession,
    workspace_id: UUID,
    conversations: list[Conversation],
    *,
    with_activity: bool = False,
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
    latest_runs = await tasks_service.latest_run_by_task(
        db, workspace_id, [t.id for t in active_tasks.values()]
    )
    working = await _working_times(db, workspace_id, list(latest_runs.values()))
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
        run = latest_runs.get(active.id) if active is not None else None
        name, role_title = (None, None)
        if conversation.primary_agent_id is not None:
            name, role_title = agents.get(conversation.primary_agent_id, (None, None))
        out.append(
            ConversationOut(
                project_id=conversation.project_id,
                workspace_version=conversation.workspace_version,
                source_conversation_id=conversation.source_conversation_id,
                source_message_id=conversation.source_message_id,
                source_checkpoint_id=conversation.source_checkpoint_id,
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
                active_run_status=run.status if run is not None else None,
                active_run_started_at=run.started_at if run is not None else None,
                active_run_working_since=(
                    working[run.id].working_since if run is not None and run.id in working else None
                ),
                active_run_working_seconds=(
                    working[run.id].working_seconds if run is not None and run.id in working else 0
                ),
                active_activity=(
                    await _active_activity(db, workspace_id, active)
                    if with_activity and active is not None
                    else None
                ),
                last_message_preview=(
                    _preview_of(last_message) if last_message is not None else None
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
    """One conversation, with ``active_activity``. Single-row callers only —
    the list projection deliberately leaves that field None."""
    return (await project_conversations(db, workspace_id, [conversation], with_activity=True))[0]


def _failure_of(message: Message, run_error: str | None = None) -> FailureNoticeOut | None:
    """The readable half of a run failure, for the row that records one.

    The stored ``content_json`` is left exactly as the worker wrote it — its
    ``text`` is the record, and rewriting a persisted row from a projection
    would make the transcript disagree with the database. This is that same
    failure said again, in the vocabulary a person reads.

    ``run_error`` is the run's own ``error_message``, and it is what the
    notice is built from wherever the caller has it. The transcript row is not
    the failure's own words: the worker writes it as ``f"Run {status}:
    {message}"``, so building the notice from the row put the run's *status
    line* into a card whose heading already says the agent could not finish —
    "Run failed: openai: HTTP 429…" under "Bisby couldn't finish that". The
    activity feed never had that problem because it reads ``error_message``
    directly; this reads the same column. The row's text remains the fallback,
    with that framing removed, for the failures that have no run at all — a
    turn that never reached the agent leaves an ``error`` row and nothing
    else.
    """
    if (
        message.sender_type != SenderType.SYSTEM.value
        or message.message_type != MessageType.ERROR.value
    ):
        return None
    content = message.content_json if isinstance(message.content_json, dict) else {}
    code = content.get("error_code")
    text = run_error if run_error is not None and run_error.strip() else _message_text(content)
    notice = failure_notice(code if isinstance(code, str) else None, text)
    return FailureNoticeOut(
        code=notice.code,
        summary=notice.summary,
        detail=notice.detail,
        reference=notice.reference,
    )


def _preview_of(message: Message) -> str | None:
    """The one line that stands for this conversation in a list.

    A failure's raw ``text`` is a note to whoever owns the incident — "Run
    failed: tool call a34dd1dc-… execution outcome is unknown; manual
    reconciliation is required" — and the preview is the widest surface it
    has: the chat rail, the agent page and the attention inbox all show this
    string, so the sentence the failure card exists to replace was still the
    first thing a person read all day. The row keeps its text; this is the
    same failure in the same ``failure_notice`` vocabulary the card uses,
    said once and read everywhere.

    Only the summary. ``detail`` is a provider's sentence or a command's
    stderr tail, which belongs on the card where there is room for it, and
    ``reference`` is an identifier — the exact thing that made the original
    unreadable at 160 characters.
    """
    notice = _failure_of(message)
    text = notice.summary if notice is not None else _message_text(message.content_json)
    from jhin_secrets.intake import redact_legacy_text

    return _truncate(redact_legacy_text(text), PREVIEW_CHARS) or None


async def _failure_texts(
    db: AsyncSession, workspace_id: UUID, messages: list[Message]
) -> dict[UUID, str]:
    """What the failed runs behind these rows actually said, by run id.

    One query for the whole page, and only when the page contains a failure —
    which most do not.
    """
    run_ids = {
        message.run_id
        for message in messages
        if message.run_id is not None
        and message.sender_type == SenderType.SYSTEM.value
        and message.message_type == MessageType.ERROR.value
    }
    if not run_ids:
        return {}
    rows = await db.execute(
        select(AgentRun.id, AgentRun.error_message).where(
            AgentRun.workspace_id == workspace_id,
            AgentRun.id.in_(run_ids),
            AgentRun.error_message.is_not(None),
        )
    )
    return {row[0]: row[1] for row in rows.all()}


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
    run_errors = await _failure_texts(db, workspace_id, messages)
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
                failure=_failure_of(
                    message,
                    run_errors.get(message.run_id) if message.run_id is not None else None,
                ),
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
    offset = max(offset, 0)
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
    agent_out: ConversationAgentOut | None = None
    if agent is not None:
        persona = await personas_service.persona_for_agent(db, agent)
        agent_out = ConversationAgentOut.model_validate(agent).model_copy(
            update={
                "persona": (
                    AgentPersonaSummary.from_record(persona) if persona is not None else None
                )
            }
        )
    return ConversationDetailOut(
        conversation=await project_conversation(db, workspace_id, conversation),
        agent=agent_out,
        tasks=[TaskOut.model_validate(t) for t in tasks],
        total_input_tokens=sum(r.input_tokens for r in runs),
        total_output_tokens=sum(r.output_tokens for r in runs),
        total_cost_micros=sum(r.estimated_cost_micros for r in runs),
        pending_approvals=[ApprovalOut.model_validate(a) for a in approvals],
        resume=await _resume_offer(db, workspace_id, conversation, tasks, agent),
    )


async def list_tool_calls(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    *,
    before: UUID | None = None,
    limit: int = 100,
) -> ConversationToolCallListOut:
    """All gateway actions across every chat run, bounded and paginated."""
    await get_conversation(db, workspace_id, conversation_id)
    limit = max(1, min(100, limit))
    conditions = []
    if before is not None:
        anchor = await get_tool_call(db, workspace_id, conversation_id, before)
        conditions.append(
            or_(
                ToolCall.created_at < anchor.created_at,
                and_(ToolCall.created_at == anchor.created_at, ToolCall.id < before),
            )
        )
    rows = list(
        await db.execute(
            select(ToolCall, Task.id, Agent.name)
            .join(
                AgentRun,
                and_(AgentRun.id == ToolCall.run_id, AgentRun.workspace_id == workspace_id),
            )
            .join(Task, and_(Task.id == AgentRun.task_id, Task.workspace_id == workspace_id))
            .outerjoin(
                Agent, and_(Agent.id == ToolCall.agent_id, Agent.workspace_id == workspace_id)
            )
            .where(
                ToolCall.workspace_id == workspace_id,
                Task.conversation_id == conversation_id,
                *conditions,
            )
            .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
            .limit(limit + 1)
        )
    )
    recent = list(reversed(rows[:limit]))
    projected = await project_tool_calls(db, workspace_id, [row[0] for row in recent])
    return ConversationToolCallListOut(
        items=[
            ConversationToolCallOut(**call.model_dump(), task_id=row[1], agent_name=row[2])
            for call, row in zip(projected, recent, strict=True)
        ],
        has_more=len(rows) > limit,
        limit=limit,
        next_before=recent[0][0].id if len(rows) > limit and recent else None,
    )


async def get_tool_call(
    db: AsyncSession, workspace_id: UUID, conversation_id: UUID, tool_call_id: UUID
) -> ConversationToolCallOut:
    await get_conversation(db, workspace_id, conversation_id)
    row = (
        await db.execute(
            select(ToolCall, Task.id, Agent.name)
            .join(
                AgentRun,
                and_(AgentRun.id == ToolCall.run_id, AgentRun.workspace_id == workspace_id),
            )
            .join(Task, and_(Task.id == AgentRun.task_id, Task.workspace_id == workspace_id))
            .outerjoin(
                Agent, and_(Agent.id == ToolCall.agent_id, Agent.workspace_id == workspace_id)
            )
            .where(
                ToolCall.id == tool_call_id,
                ToolCall.workspace_id == workspace_id,
                Task.conversation_id == conversation_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Tool call not found in this conversation")
    call = (await project_tool_calls(db, workspace_id, [row[0]]))[0]
    return ConversationToolCallOut(**call.model_dump(), task_id=row[1], agent_name=row[2])


async def list_messages(
    db: AsyncSession,
    workspace_id: UUID,
    conversation_id: UUID,
    *,
    after: UUID | None = None,
    limit: int | None = None,
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
    query = query.order_by(Message.created_at, Message.id)
    if limit is not None:
        query = query.limit(limit)
    rows = await db.scalars(query)
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
    project_id: UUID | None = None,
    execution_mode: str = "act",
    model_profile_id: UUID | None = None,
    crypto: SecretCrypto | None = None,
    secure_inputs: list[dict[str, Any]] | None = None,
) -> tuple[Conversation, TurnResult | None]:
    agent = await _require_active_agent(db, ctx.workspace_id, agent_id)
    # Serialize create retries before allocating a conversation or capture.
    await db.scalar(
        select(Workspace.id).where(Workspace.id == ctx.workspace_id).with_for_update(key_share=True)
    )
    if client_turn_id:
        existing_chat = await db.scalar(
            select(Conversation)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.workspace_id == ctx.workspace_id,
                Conversation.created_by_user_id == ctx.user.id,
                Conversation.primary_agent_id == agent.id,
                Message.sender_type == "user",
                Message.content_json["client_turn_id"].as_string() == client_turn_id,
            )
            .limit(1)
        )
        if existing_chat is not None:
            return existing_chat, await _existing_turn(db, existing_chat, client_turn_id)
    title_inputs = [
        {"value": (title or "")[start:end]} for start, end, _ in secret_spans(title or "")
    ]
    secure_inputs = [*(secure_inputs or []), *title_inputs]
    if (secret_spans(text or "") or secure_inputs) and crypto is None:
        raise HTTPException(
            503, "Secure storage is unavailable; configure encryption before sending credentials"
        )
    if project_id is not None:
        from jhin_api.chat_files.service import project

        await project(db, ctx.workspace_id, project_id)
    conversation = Conversation(
        workspace_id=ctx.workspace_id,
        title=safe_input_text(title or "").strip()[:200]
        or default_title(safe_input_text(text or ""), agent.name),
        primary_agent_id=agent.id,
        created_by_user_id=ctx.user.id,
        last_activity_at=_now(),
        project_id=project_id,
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
    turn: TurnResult | None = None
    if text is not None or secure_inputs:
        turn = await _run_turn(
            db,
            ctx,
            temporal,
            conversation,
            agent,
            text=text or "",
            client_turn_id=client_turn_id,
            request_id=request_id,
            ip_hash=ip_hash,
            publisher=publisher,
            execution_mode=execution_mode,
            model_profile_id=model_profile_id,
            crypto=crypto,
            secure_inputs=secure_inputs,
        )
    else:
        await db.commit()
    await _publish(
        publisher,
        ctx.workspace_id,
        "conversation.created",
        {"conversation_id": str(conversation.id), "agent_id": str(agent.id)},
    )
    return conversation, turn


def _require_chat_authority(ctx: WorkspaceContext, conversation: Conversation) -> None:
    """Members act on their own chats; admins act on anyone's.

    A chat is a person's own workspace, not shared configuration: a colleague
    should not be able to rename or delete the thread you are mid-conversation
    in. Admins keep a way in because someone has to be able to clean up after
    a departed teammate (docs/architecture/rbac.md).
    """
    if role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        return
    if conversation.created_by_user_id == ctx.user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the person who started this chat, or an admin, can change it",
    )


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
    _require_chat_authority(ctx, conversation)
    changed: dict[str, Any] = {}
    if "project_id" in values:
        if values["project_id"] is not None:
            from jhin_api.chat_files.service import project

            await project(db, ctx.workspace_id, values["project_id"])
        conversation.project_id = values["project_id"]
        changed["project_id"] = str(conversation.project_id) if conversation.project_id else None
    if (title := values.get("title")) is not None:
        conversation.title = safe_input_text(title).strip()[:200] or conversation.title
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
    _require_chat_authority(ctx, conversation)
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
    execution_mode: str | None = None,
    delivery: str = "auto",
    model_profile_id: UUID | None = None,
    attachment_ids: list[UUID] | None = None,
    context_refs: list[dict[str, Any]] | None = None,
    crypto: SecretCrypto | None = None,
    secure_inputs: list[dict[str, Any]] | None = None,
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
        execution_mode=execution_mode,
        delivery=delivery,
        model_profile_id=model_profile_id,
        attachment_ids=attachment_ids,
        context_refs=context_refs,
        crypto=crypto,
        secure_inputs=secure_inputs,
    )


async def branch(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    *,
    message_id: UUID,
    checkpoint_id: UUID | None,
    title: str | None,
) -> dict[str, Any]:
    from jhin_api.chat_files.service import create_checkpoint, get_checkpoint
    from jhin_db.models import FileCheckpoint
    from jhin_media.managed_files import (
        FileAccessError,
        clone_attachment_references,
        clone_checkpoint_files,
    )

    source = await get_conversation(db, ctx.workspace_id, conversation_id)
    _require_chat_authority(ctx, source)
    messages = await list_messages(db, ctx.workspace_id, source.id, limit=1001)
    position = next((i for i, message in enumerate(messages) if message.id == message_id), None)
    if position is None:
        if len(messages) == 1001:
            raise HTTPException(422, "A branch supports up to 1000 messages")
        raise HTTPException(404, "Branch message not found in this conversation")
    if position > 999:
        raise HTTPException(422, "A branch supports up to 1000 messages")
    anchor = messages[position]
    checkpoint: FileCheckpoint | None
    if checkpoint_id is not None:
        checkpoint = await get_checkpoint(db, ctx.workspace_id, source.id, checkpoint_id)
    elif position == len(messages) - 1:
        if _active_task(await _conversation_tasks(db, ctx.workspace_id, source.id)) is not None:
            raise HTTPException(
                409, "Wait for the current turn to finish or select a saved checkpoint"
            )
        checkpoint = await create_checkpoint(db, ctx, source.id, "Branch checkpoint")
    else:
        checkpoint = await db.scalar(
            select(FileCheckpoint)
            .where(
                FileCheckpoint.workspace_id == ctx.workspace_id,
                FileCheckpoint.conversation_id == source.id,
                FileCheckpoint.created_at <= anchor.created_at,
            )
            .order_by(FileCheckpoint.created_at.desc(), FileCheckpoint.id.desc())
            .limit(1)
        )
        if checkpoint is None:
            raise HTTPException(
                409,
                "No saved checkpoint exists at this message. Select a checkpoint "
                "explicitly or branch from the latest message.",
            )
    target = Conversation(
        workspace_id=ctx.workspace_id,
        title=(title or f"Branch: {source.title}")[:200],
        project_id=source.project_id,
        primary_agent_id=source.primary_agent_id,
        created_by_user_id=ctx.user.id,
        last_activity_at=_now(),
        source_conversation_id=source.id,
        source_message_id=anchor.id,
        source_checkpoint_id=checkpoint.id,
    )
    db.add(target)
    await db.flush()
    copied = await clone_checkpoint_files(db, ctx.workspace_id, source.id, target.id, checkpoint.id)
    # A branch uses the selected checkpoint, even if its inherited project's
    # starter revision differs or the original chat is subsequently removed.
    db.add(
        AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type="user",
            actor_id=ctx.user.id,
            action="chat.project.seeded",
            target_type="conversation",
            target_id=target.id,
            metadata_json={"source_kind": "branch", "source_checkpoint_id": str(checkpoint.id)},
        )
    )
    for original in messages[: position + 1]:
        content = {**original.content_json, "branched_from_message_id": str(original.id)}
        content.pop("_human_authority", None)
        if references := content.get("attachments"):
            try:
                content["attachments"] = await clone_attachment_references(
                    db, ctx.workspace_id, target.id, references
                )
            except FileAccessError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
        db.add(
            Message(
                workspace_id=ctx.workspace_id,
                conversation_id=target.id,
                sender_type=original.sender_type,
                sender_id=original.sender_id,
                recipient_type=original.recipient_type,
                recipient_id=original.recipient_id,
                message_type=original.message_type,
                content_json=content,
                visibility="visible",
                created_at=original.created_at,
            )
        )
    await db.commit()
    from jhin_api.runtime.service import seed_branch_workspace

    await seed_branch_workspace(db, ctx, target.id, copied.manifest_json)
    return {
        "conversation_id": str(target.id),
        "checkpoint_id": str(copied.id),
        "source_message_id": str(anchor.id),
    }


async def control(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    conversation_id: UUID,
    *,
    action: str,
    request_id: UUID,
    ip_hash: str,
) -> dict[str, Any]:
    chat = await get_conversation(db, ctx.workspace_id, conversation_id)
    _require_chat_authority(ctx, chat)
    tasks = await _conversation_tasks(db, ctx.workspace_id, conversation_id)
    active = _active_task(tasks)
    if active is None:
        raise HTTPException(409, "This conversation has no active turn")
    signal = {"stop": "cancel", "pause": "pause", "resume": "resume"}.get(action)
    if signal is None:
        raise HTTPException(422, "Unknown conversation control")
    task = await tasks_service.signal_task(
        db,
        ctx,
        temporal,
        active.id,
        signal=signal,
        action=f"task.{action}_requested",
        request_id=request_id,
        ip_hash=ip_hash,
    )
    return {
        "task_id": str(task.id),
        "status": "stopping"
        if action == "stop"
        else ("pausing" if action == "pause" else "resuming"),
    }


async def change_queued(
    db: AsyncSession,
    ctx: WorkspaceContext,
    conversation_id: UUID,
    task_id: UUID,
    *,
    text: str | None,
    crypto: SecretCrypto | None = None,
    secure_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    chat = await get_conversation(db, ctx.workspace_id, conversation_id)
    _require_chat_authority(ctx, chat)
    await db.scalar(
        select(Workspace).where(Workspace.id == ctx.workspace_id).with_for_update(key_share=True)
    )
    task = await db.scalar(
        select(Task)
        .where(
            Task.id == task_id,
            Task.workspace_id == ctx.workspace_id,
            Task.conversation_id == conversation_id,
        )
        .with_for_update()
    )
    has_run = await db.scalar(
        select(AgentRun.id)
        .where(AgentRun.task_id == task_id, AgentRun.workspace_id == ctx.workspace_id)
        .limit(1)
    )
    if task is None:
        raise HTTPException(404, "Queued turn not found")
    if task.state != "queued" or has_run is not None:
        raise HTTPException(409, "This turn has already started")
    message = await db.scalar(
        select(Message)
        .where(
            Message.task_id == task.id,
            Message.workspace_id == ctx.workspace_id,
            Message.sender_type == "user",
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    if text is None:
        task.state = "cancelled"
        task.metadata_json = {**task.metadata_json, "stop_requested_at": _now().isoformat()}
        if message is not None:
            message.content_json = {**message.content_json, "delivery": "cancelled"}
    else:
        capture_agent_id = task.assigned_agent_id or chat.primary_agent_id
        if capture_agent_id is None:
            raise HTTPException(409, "This queued turn has no assigned agent")
        try:
            captured = await capture_input(
                db,
                crypto,
                workspace_id=ctx.workspace_id,
                conversation_id=chat.id,
                agent_id=capture_agent_id,
                user_id=ctx.user.id,
                text=text,
                secure_inputs=secure_inputs,
            )
        except VariableError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        text = captured.text
        task.description = text
        task.title = default_title(text, "Agent")[:500]
        if message is not None:
            from jhin_api.human_authority import human_content

            message.sender_id = ctx.user.id
            message.content_json = human_content(ctx, {**message.content_json, "text": text})
            if captured.references:
                message.content_json = {
                    **message.content_json,
                    "secure_inputs": captured.references,
                }
        task.metadata_json = merge_capture_metadata(task.metadata_json or {}, captured)
    await db.commit()
    return {"task_id": str(task.id), "status": task.state, "text": task.description}


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


async def _pin_named_context(
    db: AsyncSession, workspace_id: UUID, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    from jhin_db.models import ChatProject, Connection

    pinned = []
    seen: set[tuple[str, UUID]] = set()
    for ref in references:
        kind = ref.get("type")
        if kind in ("file", "artifact"):
            continue
        if kind not in ("agent", "app", "project"):
            raise HTTPException(422, "Unknown context reference type")
        try:
            identity = UUID(str(ref["id"]))
        except (ValueError, KeyError):
            raise HTTPException(422, "Invalid context reference") from None
        if (kind, identity) in seen:
            continue
        seen.add((kind, identity))
        model: Any = {"agent": Agent, "app": Connection, "project": ChatProject}[kind]
        row = await db.scalar(
            select(model).where(model.id == identity, model.workspace_id == workspace_id)
        )
        if row is None or (kind == "project" and row.archived):
            raise HTTPException(404, "Selected context not found")
        context = (
            row.context[:8_000]
            if kind == "project"
            else (row.role_title if kind == "agent" else row.connector_type)
        )
        pinned.append(
            {"type": kind, "id": str(identity), "label": row.name[:200], "context": context}
        )
    return pinned


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
    execution_mode: str | None = None,
    delivery: str = "auto",
    model_profile_id: UUID | None = None,
    attachment_ids: list[UUID] | None = None,
    context_refs: list[dict[str, Any]] | None = None,
    crypto: SecretCrypto | None = None,
    secure_inputs: list[dict[str, Any]] | None = None,
) -> TurnResult:
    # Serialize competing submissions before the idempotency check. Row lock
    # persists through the message/task commit; UUID ordering is not a lock.
    await db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation.id, Conversation.workspace_id == ctx.workspace_id)
        .with_for_update()
    )
    if client_turn_id:
        existing = await _existing_turn(db, conversation, client_turn_id)
        if existing is not None:
            return existing

    try:
        captured = await capture_input(
            db,
            crypto,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation.id,
            agent_id=agent.id,
            user_id=ctx.user.id,
            text=text,
            secure_inputs=secure_inputs,
        )
    except VariableError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    text = captured.text
    from jhin_api.human_authority import human_content

    content: dict[str, Any] = human_content(ctx, {"text": text})
    if captured.references:
        content["secure_inputs"] = captured.references
    if attachment_ids or context_refs:
        from jhin_media.managed_files import FileAccessError, pin_attachments

        try:
            content["attachments"] = await pin_attachments(
                db,
                ctx.workspace_id,
                conversation.id,
                attachment_ids or [],
                context_refs=context_refs or [],
            )
        except FileAccessError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
    selected_context = list(context_refs or [])
    if conversation.project_id and not any(
        ref.get("type") == "project" and str(ref.get("id")) == str(conversation.project_id)
        for ref in selected_context
    ):
        selected_context.append({"type": "project", "id": str(conversation.project_id)})
    if selected_context:
        content["context_refs"] = await _pin_named_context(db, ctx.workspace_id, selected_context)
    if not text.strip():
        text = "Please work with the attached files."
        content["text"] = text
    if execution_mode is not None and execution_mode not in ("ask", "plan", "act"):
        raise HTTPException(422, "Invalid execution mode")
    if delivery not in ("auto", "steer", "queue"):
        raise HTTPException(422, "Invalid turn delivery")
    if model_profile_id is not None:
        from jhin_db.models import ModelProfile

        if (
            await db.scalar(
                select(ModelProfile.id).where(
                    ModelProfile.id == model_profile_id,
                    ModelProfile.workspace_id == ctx.workspace_id,
                )
            )
            is None
        ):
            raise HTTPException(404, "Model profile not found")
    if client_turn_id:
        content["client_turn_id"] = client_turn_id
    tasks = await _conversation_tasks(db, ctx.workspace_id, conversation.id)
    active = _active_task(tasks)
    metadata: dict[str, Any] = {
        "origin": "conversation",
        "conversation_id": str(conversation.id),
        "execution_mode": execution_mode or "act",
        "delivery": delivery,
    }
    metadata = merge_capture_metadata(metadata, captured)
    continued = await _continued_assignment_id(
        db, ctx.workspace_id, conversation, agent, _predecessor_turn(tasks)
    )
    if continued is not None:
        metadata["editorial_assignment_id"] = continued
    if model_profile_id is not None:
        metadata["model_profile_id"] = str(model_profile_id)
    if content.get("attachments"):
        metadata["attachments"] = content["attachments"]
    if content.get("context_refs"):
        metadata["context_refs"] = content["context_refs"]
    if delivery == "queue" and active is not None:
        predecessor = next(
            (
                t
                for t in tasks
                if t.state in tasks_service.ACTIVE_TASK_STATES
                and t.parent_task_id is None
                and t.metadata_json.get("origin") != "work_request"
            ),
            active,
        )
        metadata["queue_after_task_id"] = str(predecessor.id)
        active = None
    if active is not None and (
        (
            execution_mode is not None
            and execution_mode != active.metadata_json.get("execution_mode", "act")
        )
        or model_profile_id is not None
    ):
        raise HTTPException(
            409, "Queue a new turn to change the mode or model while work is active"
        )

    mode: TurnMode = "instruction"
    if active is not None:
        active.metadata_json = merge_capture_metadata(active.metadata_json or {}, captured)
        content["delivery"] = "pending"
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
        try:
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
            await db.execute(
                update(Message)
                .where(
                    Message.id == message.id,
                    Message.content_json["delivery"].as_string() == "pending",
                )
                .values(content_json={**message.content_json, "delivery": "delivered"})
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
            # The run finished between our activity check and the signal, so
            # nothing will ever read this instruction. Promote the turn to a
            # fresh work episode instead of stranding the person's message
            # under a task that is already over.
            task = Task(
                workspace_id=ctx.workspace_id,
                title=default_title(text, agent.name)[:500],
                description=text,
                assigned_agent_id=agent.id,
                conversation_id=conversation.id,
                correlation_id=new_uuid7(),
                metadata_json=metadata,
            )
            db.add(task)
            await db.flush()
            message.task_id = task.id
            message.message_type = MessageType.TEXT.value
            mode = "new_task"
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
            metadata_json=metadata,
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


async def _seed_message(db: AsyncSession, workspace_id: UUID, task: Task) -> Message | None:
    """The person's own words that started this turn, if the row still exists."""
    seed: Message | None = await db.scalar(
        select(Message)
        .where(
            Message.workspace_id == workspace_id,
            Message.task_id == task.id,
            Message.sender_type == SenderType.USER.value,
            Message.message_type.in_((MessageType.TEXT.value, MessageType.INSTRUCTION.value)),
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    return seed


async def _linked_task(
    db: AsyncSession, workspace_id: UUID, conversation: Conversation, raw_id: Any
) -> Task | None:
    """The task one end of a resume link points at, if it is really there.

    The id comes out of a task's own ``metadata_json``, which is the platform's
    writing rather than anyone else's — the workspace and conversation are
    checked anyway, because a link that survived a task being moved or a
    conversation being deleted should read as a broken link and offer the turn
    back, not as a successor that lives somewhere else.
    """
    task_id = _as_uuid(raw_id)
    if task_id is None:
        return None
    linked: Task | None = await db.scalar(
        select(Task).where(
            Task.id == task_id,
            Task.workspace_id == workspace_id,
            Task.conversation_id == conversation.id,
        )
    )
    return linked


def _nothing_to_resume() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="There's nothing to pick up here — the last turn didn't fail.",
    )


async def resume_conversation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    conversation_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
    publisher: EventPublisher | None = None,
    crypto: SecretCrypto | None = None,
) -> ResumeResult:
    """Pick the conversation's failed turn back up, without retyping it.

    A fresh work episode carries the same question, in the same thread. Not a
    restart of the old one: the failure keeps its row, its activity card and
    its place in the transcript, because a person who comes back tomorrow is
    entitled to see that it happened. And not a parallel conversation either —
    the thread is where the context is.

    The person's own message is *moved* onto the new episode rather than
    copied. It has to be one or the other, and copying is worse in both
    directions: the transcript would show their words twice, and the agent
    worker decides the prompt shape by checking that this task's first turn is
    the person's message (``_is_chat_turn``) — a task without one gets the
    question restated as a brief ahead of everything said earlier, which is
    the shape that has agents answering the previous question.

    Safe to press twice. The claim is the ``resumed_by_task_id`` stamp written
    under a row lock in the same transaction as the new task, so a second
    press finds the successor and returns it rather than starting another.
    """
    conversation = await get_conversation(db, ctx.workspace_id, conversation_id)
    _require_chat_authority(ctx, conversation)
    tasks = await _conversation_tasks(db, ctx.workspace_id, conversation_id)
    newest = next((task for task in tasks if _is_turn_task(task)), None)
    if newest is None:
        raise _nothing_to_resume()

    if newest.state != TaskState.FAILED.value:
        # The newest turn is already a successor somebody started: this press
        # is the second one (or a retry of a request whose response was lost),
        # and its answer is that same task.
        predecessor = await _linked_task(
            db, ctx.workspace_id, conversation, newest.metadata_json.get(RESUME_OF_KEY)
        )
        if predecessor is None:
            raise _nothing_to_resume()
        return ResumeResult(
            conversation=conversation, task=newest, resumed_task=predecessor, created=False
        )

    # Re-read under a row lock: everything decided from here on is decided
    # against this row, and the stamp that closes it is written before commit.
    #
    # ``populate_existing`` is what makes that sentence true. This session
    # already loaded the task above, so without it the ORM answers from its
    # identity map and throws the locked row's columns away — the lock is
    # taken in the database and its whole point discarded. Two overlapping
    # presses would then both read a ``metadata_json`` from before the other
    # committed, both miss ``resumed_by_task_id``, and both start a workflow:
    # the one thing the endpoint promises cannot happen.
    failed = await db.scalar(
        select(Task)
        .where(Task.id == newest.id, Task.workspace_id == ctx.workspace_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if failed is None:
        raise _nothing_to_resume()
    successor = await _linked_task(
        db, ctx.workspace_id, conversation, failed.metadata_json.get(RESUMED_BY_KEY)
    )
    if successor is not None:
        return ResumeResult(
            conversation=conversation, task=successor, resumed_task=failed, created=False
        )
    if failed.state != TaskState.FAILED.value:
        raise _nothing_to_resume()

    blocking = (await _unreconciled_calls(db, ctx.workspace_id, failed.id)).blocking
    if blocking is not None:
        # The refusal a person reads is the same sentence the offer carried,
        # so pressing a control that said "check first" cannot answer with
        # different words than the control did.
        agent_row = await _get_agent(db, ctx.workspace_id, conversation.primary_agent_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_unreconciled_reason(
                blocking[1], agent_row.name if agent_row is not None else ""
            ),
        )
    if conversation.status != ConversationStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This chat is archived. Restore it to pick this back up.",
        )
    agent = await _require_active_agent(db, ctx.workspace_id, conversation.primary_agent_id)

    seed = await _seed_message(db, ctx.workspace_id, failed)
    instruction = failed.description.strip() or _message_text(
        seed.content_json if seed is not None else {}
    )
    if not instruction:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This turn has no message left to send again. Type what you'd like instead.",
        )

    try:
        captured = await capture_input(
            db,
            crypto,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation.id,
            agent_id=agent.id,
            user_id=ctx.user.id,
            text=instruction,
        )
    except VariableError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    instruction = captured.text

    task = Task(
        workspace_id=ctx.workspace_id,
        title=safe_input_text(failed.title),
        description=instruction,
        assigned_agent_id=agent.id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
        metadata_json={
            "origin": "conversation",
            "conversation_id": str(conversation.id),
            RESUME_OF_KEY: str(failed.id),
            **{
                key: failed.metadata_json[key]
                for key in (
                    "execution_mode",
                    "model_profile_id",
                    "attachments",
                    "context_refs",
                    "secure_inputs",
                    "required_inputs",
                )
                if key in failed.metadata_json
            },
        },
    )
    task.metadata_json = merge_capture_metadata(task.metadata_json or {}, captured)
    continued = await _continued_assignment_id(db, ctx.workspace_id, conversation, agent, failed)
    if continued is not None:
        task.metadata_json = {**task.metadata_json, "editorial_assignment_id": continued}
    db.add(task)
    await db.flush()
    if seed is not None:
        seed.task_id = task.id
        seed.content_json = {**seed.content_json, "text": instruction}
        if captured.references:
            seed.content_json = {**seed.content_json, "secure_inputs": captured.references}
        # An instruction row is a mid-run steer, and the worker renders one as
        # "Additional instruction: …". As the seed of its own episode it is
        # the question, so it is filed as one -- the same promotion
        # ``_run_turn`` makes when a turn arrives too late to steer anything.
        seed.message_type = MessageType.TEXT.value
    failed.metadata_json = {**failed.metadata_json, RESUMED_BY_KEY: str(task.id)}
    db.add(
        Message(
            workspace_id=ctx.workspace_id,
            # Deliberately attached to no task: it belongs to the thread, not
            # to either episode. On the new task it would reach the model as a
            # second user turn after the question; on the old one it would be
            # a note on an episode that is over.
            task_id=None,
            conversation_id=conversation.id,
            sender_type=SenderType.SYSTEM.value,
            sender_id=None,
            recipient_type=RecipientType.USER.value,
            recipient_id=ctx.user.id,
            message_type=MessageType.NOTE.value,
            content_json={
                "kind": "turn_resumed",
                "text": f"Trying “{_truncate(safe_input_text(failed.title), 80)}” again.",
                "resumed_task_id": str(failed.id),
                # Not ``task_id``: the timeline reads that key on other
                # message kinds to decide what belongs to which exchange, and
                # this row belongs to no episode at all.
                "into_task_id": str(task.id),
            },
            visibility=MessageVisibility.VISIBLE.value,
        )
    )
    conversation.last_activity_at = _now()
    audit.record(
        db,
        action="conversation.resumed",
        target_type="conversation",
        target_id=conversation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"task_id": str(task.id), "resumed_task_id": str(failed.id)},
    )
    await db.commit()
    # Commit before start so the worker's activities always find the row. A
    # Temporal outage marks the new task failed and raises 503, which leaves
    # the newest turn failed again -- offered back rather than stranded.
    await tasks_service.start_workflow(db, temporal, task, agent.id, instruction)
    await _publish(
        publisher,
        ctx.workspace_id,
        "conversation.resumed",
        {
            "conversation_id": str(conversation.id),
            "task_id": str(task.id),
            "resumed_task_id": str(failed.id),
        },
    )
    return ResumeResult(conversation=conversation, task=task, resumed_task=failed, created=True)


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


async def _latest_run_failures(
    db: AsyncSession, workspace_id: UUID, task_ids: list[UUID]
) -> dict[UUID, FailureNotice]:
    """Latest run failure per failed task, as readable copy.

    The stored ``error_message`` is written for whoever debugs the run and is
    already redacted upstream; it is passed to :func:`failure_notice` and
    never appended to a card as it stands. A run that recorded only a code
    still produces a notice, because a code is enough to say a sentence and
    silence is worse than a short answer.
    """
    if not task_ids:
        return {}
    rows = await db.execute(
        select(AgentRun.task_id, AgentRun.error_code, AgentRun.error_message)
        .where(
            AgentRun.workspace_id == workspace_id,
            AgentRun.task_id.in_(task_ids),
            or_(AgentRun.error_message.is_not(None), AgentRun.error_code.is_not(None)),
        )
        .order_by(AgentRun.task_id, AgentRun.created_at)
    )
    latest: dict[UUID, FailureNotice] = {}
    for task_id, code, message in rows.all():
        if task_id is None or not (code or message):
            continue
        # Later rows overwrite: last write wins.
        latest[task_id] = failure_notice(code, message or "")
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
    failure_notices = await _latest_run_failures(
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
            notice = (
                failure_notices.get(card_task.id) if draft.kind is ActivityKind.FAILED else None
            )
            if notice is not None:
                # The card's first sentence names the agent and the work; this
                # is the second one, saying what went wrong. The failure's own
                # words win where it has any -- a provider saying "you
                # exceeded your current quota" is the actual information, and
                # Jhin restating it more vaguely would help nobody. Where it
                # has none, the notice supplies the sentence, which is the
                # whole point: those are the failures whose original text was
                # internal vocabulary and an identifier.
                summary = f"{summary} {notice.detail or notice.summary}"
                detail_json = {
                    **detail_json,
                    "error_code": notice.code,
                    "error_message": notice.detail,
                    "error_reference": notice.reference,
                }
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
    # A run parked on a question needs the person exactly as much as one
    # parked on an approval does. The question itself lives in its chat, so
    # the badge only has to be honest about which chat is waiting.
    waiting = [
        c
        for c in projected
        if c.active_run_status in (RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_PERSON.value)
    ]
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
    # Workspace budget notice (plan 15.5): one lightweight card when tracked
    # month spend crossed the warning threshold. Informational — it never
    # bumps ``total`` (the nav badge), unlike decisions waiting on a person.
    budget_notice: BudgetNoticeOut | None = None
    workspace = await db.get(Workspace, workspace_id)
    budget_micros, threshold = workspace_budget_settings(
        workspace.settings_json if workspace is not None else None
    )
    if budget_micros:
        spent = await month_spend_micros(db, workspace_id)
        if spent >= threshold * budget_micros:
            budget_notice = BudgetNoticeOut(
                monthly_budget_micros=budget_micros,
                spent_month_micros=spent,
                percent_used=int(spent * 100 / budget_micros),
                warning_threshold=threshold,
            )
    counts = AttentionCounts(
        approvals=len(approvals),
        failures=len(failed),
        reviews=len(pending_reviews),
        reviews_in_progress=len(reviews_in_progress),
        budget_warnings=1 if budget_notice is not None else 0,
        total=len(approvals) + len(failed) + len(waiting) + len(pending_reviews),
    )
    return AttentionOut(
        pending_approvals=[ApprovalOut.model_validate(a) for a in approvals],
        failed_tasks=[TaskOut.model_validate(t) for t in failed],
        waiting_conversations=waiting,
        pending_reviews=pending_reviews,
        reviews_in_progress=reviews_in_progress,
        budget=budget_notice,
        counts=counts,
    )
