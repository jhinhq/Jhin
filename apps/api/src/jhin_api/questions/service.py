"""Reading and answering the questions agents ask people.

Write ordering is the approvals rule, for the approvals reason
(``approvals/service.py``): the row is committed *first*, because the row —
not the signal — is what the agent worker re-reads as the authority for what
was answered, and then the parked workflow is woken.

The one deliberate difference is what a failed signal means. An approval that
cannot be delivered is a stuck action an operator has to see, so
``approvals.decide`` raises. A question that cannot be delivered is a
conversation the person can simply continue in the composer, so this records
the answer, returns ``resumed=False``, and lets the UI say so in one line.

Authority is the other thing this module is careful about. A ``memory_scope``
question is answered by a person whose role decides how wide a memory their
answer may authorise; that decision is made here, from ``ctx.role``, and
written to the row. A model may cite the row afterwards but can assert
nothing, and it can never widen a memory by claiming somebody said yes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.memory.service import authority_for
from jhin_api.questions.schemas import AnswerQuestionIn, QuestionOptionOut, QuestionOut
from jhin_db.models import Agent, Message, Task, User, UserQuestion
from jhin_domain import MemoryScope, UserQuestionStatus
from jhin_memory.policy import scope_exceeds
from jhin_workflows.agent_task.shared import SIGNAL_QUESTION_ANSWER

MAX_PAGE_SIZE = 200

ANSWER_KIND_OPTION = "option"
ANSWER_KIND_OTHER = "other"

#: Why an answer to a ``memory_scope`` question authorised nothing wider than
#: the agent's own memory. Empty means it did authorise something.
REASON_FREE_TEXT = "free_text_answer"
REASON_INSUFFICIENT_AUTHORITY = "insufficient_authority"
#: A ``memory_scope`` question is validated to offer only scope values, so a
#: row that offers something else is a bug upstream. No grant, and a reason
#: that says so, rather than a 500 on the person's answer.
REASON_NOT_A_SCOPE = "not_a_scope"

_MEMORY_SCOPE_KIND = "memory_scope"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite (unit tests) hands back naive timestamps; Postgres does not."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _options(question: UserQuestion) -> list[dict[str, str]]:
    """The offered choices, normalized.

    The row is written by the ask tool under a strict schema; this reads it
    defensively anyway, because a malformed row must never turn into a 500 on
    somebody's answer.
    """
    cleaned: list[dict[str, str]] = []
    raw_options: Any = question.options_json or []
    if not isinstance(raw_options, list):
        return cleaned
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value") or "")
        if not value:
            continue
        cleaned.append(
            {
                "value": value,
                "label": str(raw.get("label") or ""),
                "detail": str(raw.get("detail") or ""),
            }
        )
    return cleaned


async def _project(
    db: AsyncSession, workspace_id: UUID, rows: list[UserQuestion]
) -> list[QuestionOut]:
    """Attach the two names a person needs to read a question, in two queries."""
    if not rows:
        return []
    agent_rows = await db.execute(
        select(Agent.id, Agent.name).where(
            Agent.workspace_id == workspace_id,
            Agent.id.in_({row.agent_id for row in rows}),
        )
    )
    agent_names = {row[0]: row[1] for row in agent_rows.all()}
    user_ids = {row.answered_by_user_id for row in rows if row.answered_by_user_id is not None}
    user_names: dict[UUID, str] = {}
    if user_ids:
        user_rows = await db.execute(
            select(User.id, User.display_name).where(User.id.in_(user_ids))
        )
        user_names = {row[0]: row[1] for row in user_rows.all()}
    return [
        QuestionOut(
            id=row.id,
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            task_id=row.task_id,
            message_id=row.message_id,
            agent_id=row.agent_id,
            agent_name=agent_names.get(row.agent_id),
            kind=row.kind,
            question=row.question,
            context=row.context,
            options=[QuestionOptionOut(**option) for option in _options(row)],
            allow_other=row.allow_other,
            status=row.status,
            asked_at=row.asked_at,
            expires_at=row.expires_at,
            answered_at=row.answered_at,
            answered_by_user_id=row.answered_by_user_id,
            answered_by_name=(
                user_names.get(row.answered_by_user_id)
                if row.answered_by_user_id is not None
                else None
            ),
            answer_kind=row.answer_kind,
            answer_option_value=row.answer_option_value,
            answer_text=row.answer_text,
            granted_scope=row.granted_scope,
            grant_denied_reason=row.grant_denied_reason,
        )
        for row in rows
    ]


async def list_questions(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    status_filter: str | None = None,
    conversation_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QuestionOut], int]:
    """Pending first, newest within each group — the approvals inbox order,
    because the only reason to read this list is to find what is still open."""
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    offset = max(offset, 0)
    query = select(UserQuestion).where(UserQuestion.workspace_id == workspace_id)
    if status_filter:
        query = query.where(UserQuestion.status == status_filter)
    if conversation_id is not None:
        query = query.where(UserQuestion.conversation_id == conversation_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pending_first = (UserQuestion.status != UserQuestionStatus.PENDING.value).asc()
    rows = list(
        await db.scalars(
            query.order_by(pending_first, UserQuestion.asked_at.desc(), UserQuestion.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return await _project(db, workspace_id, rows), int(total)


async def get_question(db: AsyncSession, workspace_id: UUID, question_id: UUID) -> QuestionOut:
    row = await db.scalar(
        select(UserQuestion).where(
            UserQuestion.id == question_id, UserQuestion.workspace_id == workspace_id
        )
    )
    if row is None:
        raise _not_found()
    return (await _project(db, workspace_id, [row]))[0]


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")


async def _agent_name(db: AsyncSession, question: UserQuestion) -> str:
    agent = await db.scalar(
        select(Agent).where(
            Agent.id == question.agent_id, Agent.workspace_id == question.workspace_id
        )
    )
    return agent.name if agent is not None else "The agent"


async def _answered_by_name(db: AsyncSession, question: UserQuestion) -> str:
    if question.answered_by_user_id is None:
        return "Somebody"
    name = await db.scalar(select(User.display_name).where(User.id == question.answered_by_user_id))
    return name or "Somebody"


def _same_answer(question: UserQuestion, payload: AnswerQuestionIn) -> bool:
    """Is this the identical answer already recorded, sent again?

    Compared per field, not by text: an ``other_text`` that happens to read
    like the recorded option label is a different answer, and must 409 rather
    than quietly succeed against somebody else's decision.
    """
    if payload.option_value is not None:
        return (
            question.answer_kind == ANSWER_KIND_OPTION
            and question.answer_option_value == payload.option_value
        )
    return (
        question.answer_kind == ANSWER_KIND_OTHER
        and question.answer_text == (payload.other_text or "").strip()
    )


def _grant_for(
    question: UserQuestion, ctx: WorkspaceContext, *, answer_kind: str, option_value: str
) -> tuple[str, str, str]:
    """What this person's answer authorises: ``(scope, authority, denied_reason)``.

    Only an option grants: a typed answer is words, not a choice among the
    scopes offered, and nothing about it says which one they meant. And the
    ceiling is the *answering* person's role, never the conversation owner's —
    a member who picks "the whole company" has their answer recorded and gets
    told an admin has to record it, which is the same rule the Memories page
    already enforces.
    """
    if question.kind != _MEMORY_SCOPE_KIND:
        return "", "", ""
    if answer_kind != ANSWER_KIND_OPTION:
        return "", "", REASON_FREE_TEXT
    try:
        chosen = MemoryScope(option_value)
    except ValueError:
        return "", "", REASON_NOT_A_SCOPE
    authority = authority_for(ctx.role)
    if authority is None or scope_exceeds(chosen, authority):
        return "", "", REASON_INSUFFICIENT_AUTHORITY
    return chosen.value, authority.value, ""


async def _stamp_message(
    db: AsyncSession, question: UserQuestion, *, answered_by_name: str
) -> None:
    """Mutate the chat card in the same transaction as the row it mirrors.

    Keys are added, never rewritten, with one exception: ``text`` is what
    other consumers (the activity feed) render, and leaving it saying the
    agent is still asking after somebody answered would be a lie. ``question``
    and ``options`` are never touched — the card must always show what was
    actually asked.
    """
    if question.message_id is None:
        return
    message = await db.get(Message, question.message_id)
    if message is None or message.workspace_id != question.workspace_id:
        return
    existing = message.content_json if isinstance(message.content_json, dict) else {}
    answer = question.answer_text
    message.content_json = {
        **existing,
        "status": UserQuestionStatus.ANSWERED.value,
        "answer_kind": question.answer_kind,
        "answer_option_value": question.answer_option_value,
        "answer": answer,
        "answered_by_name": answered_by_name,
        "answered_at": question.answered_at.isoformat() if question.answered_at else "",
        "text": f"{question.question} — {answered_by_name} answered: {answer}",
    }


async def _signal_answer(
    temporal: TemporalClient, db: AsyncSession, question: UserQuestion
) -> bool:
    """Wake the parked run. The signal carries the question id and nothing
    else: the activity re-reads the row, which is the authority."""
    if question.task_id is None:
        return False
    task = await db.scalar(select(Task).where(Task.id == question.task_id))
    if task is None or task.temporal_workflow_id is None:
        return False
    handle = temporal.get_workflow_handle(task.temporal_workflow_id)
    try:
        await handle.signal(SIGNAL_QUESTION_ANSWER, args=[str(question.id)])
    except (RPCError, TemporalError, OSError):
        # Recorded but undeliverable: the run had already stopped waiting.
        # The caller turns this into one line telling the person to say it in
        # the composer instead, which is a thing they can actually do.
        return False
    return True


async def answer(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    question_id: UUID,
    payload: AnswerQuestionIn,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[QuestionOut, bool]:
    question = await db.scalar(
        select(UserQuestion)
        .where(UserQuestion.id == question_id, UserQuestion.workspace_id == ctx.workspace_id)
        .with_for_update()
    )
    if question is None:
        raise _not_found()

    if question.status == UserQuestionStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{await _agent_name(db, question)} stopped waiting for this. "
                "Send it as a message instead."
            ),
        )

    if question.status == UserQuestionStatus.ANSWERED.value:
        if not _same_answer(question, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{await _answered_by_name(db, question)} already answered this.",
            )
        # Release the row lock, then retry the same idempotent wake-up. This
        # repairs a commit -> signal failure without recording a second answer
        # or letting a different one win.
        await db.commit()
        resumed = await _signal_answer(temporal, db, question)
        return (await _project(db, ctx.workspace_id, [question]))[0], resumed

    # pending or expired. An expired question is recorded rather than refused:
    # the person's intent is real, the agent should still hear it if it can,
    # and expires_at < answered_at is the durable record that it was late.
    options = _options(question)
    if payload.option_value is not None:
        chosen = next((o for o in options if o["value"] == payload.option_value), None)
        if chosen is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown option '{payload.option_value}'",
            )
        answer_kind = ANSWER_KIND_OPTION
        option_value = chosen["value"]
        # The label, so the row reads as a sentence on its own without
        # anybody having to resolve the machine key back to what it meant.
        answer_text = chosen["label"]
    else:
        if not question.allow_other:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="This question does not take a typed answer",
            )
        answer_kind = ANSWER_KIND_OTHER
        option_value = ""
        answer_text = (payload.other_text or "").strip()

    granted_scope, granted_authority, denied_reason = _grant_for(
        question, ctx, answer_kind=answer_kind, option_value=option_value
    )

    now = _now()
    question.status = UserQuestionStatus.ANSWERED.value
    question.answer_kind = answer_kind
    question.answer_option_value = option_value
    question.answer_text = answer_text
    question.answered_at = now
    question.answered_by_user_id = ctx.user.id
    question.granted_scope = granted_scope
    question.granted_authority = granted_authority
    question.grant_denied_reason = denied_reason

    await _stamp_message(db, question, answered_by_name=ctx.user.display_name)
    audit.record(
        db,
        action="question.answered",
        target_type="user_question",
        target_id=question.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        # Never the question or the answer text: what was said lives in the
        # message and the question row, so forgetting a conversation forgets
        # it. The audit records that it happened and what it authorised.
        metadata={
            "kind": question.kind,
            "answer_kind": answer_kind,
            "option_value": option_value,
            "granted_scope": granted_scope,
            "grant_denied_reason": denied_reason,
            "late": _aware(question.expires_at) < now,
        },
    )
    # Commit before signaling: the delivery activity re-reads this row as the
    # sole authority for what was answered.
    await db.commit()

    resumed = await _signal_answer(temporal, db, question)
    return (await _project(db, ctx.workspace_id, [question]))[0], resumed
