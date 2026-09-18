"""Ground memory in a human statement or a verified native-tool fact.

An assistant's claim, a shell log, or an external HTTP failure is not evidence
that a service was configured. Scope is checked before inspecting any source.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from jhin_db.models import AgentRun, MemoryRecord, Message, ToolCall
from jhin_memory.types import MAX_CANDIDATE_CHARS, MemoryCandidate, SourceFacts
from jhin_secrets.intake import secret_spans


def normalized(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".!?")


def _credible_text(value: str) -> bool:
    text = normalized(value)
    return bool(text) and not (
        "secure_input:" in text
        or secret_spans(value)
        or re.search(r"\b(403|429|rate.limit|curl|http error|command not found)\b", text)
    )


@dataclass(frozen=True)
class VerifiedToolFact:
    content: str
    tool_call_id: str


@dataclass(frozen=True)
class HumanStatementExcerpt:
    content: str
    message_id: str


async def human_statement_excerpts(
    db: AsyncSession, source: SourceFacts, *, limit: int = 4
) -> list[HumanStatementExcerpt]:
    """Offer exact current-turn wording, never reconstruct or truncate its conditions.

    Evidence checks may look back through the conversation, but recovery must
    not encourage memorising unrelated older messages. Only the current task
    (or the cited source message) supplies these bounded suggestions.
    """
    if source.internal or limit < 1 or not (source.ref.task_id or source.ref.message_id):
        return []
    query = select(Message).where(
        Message.workspace_id == source.workspace_id,
        Message.sender_type == "user",
        Message.visibility == "visible",
    )
    if source.ref.task_id:
        query = query.where(Message.task_id == source.ref.task_id)
    else:
        query = query.where(Message.id == source.ref.message_id)
    if source.ref.conversation_id:
        query = query.where(Message.conversation_id == source.ref.conversation_id)
    if source.ref.message_id:
        origin = await db.get(Message, source.ref.message_id)
        if (
            origin is None
            or origin.workspace_id != source.workspace_id
            or origin.visibility != "visible"
            or (source.ref.task_id and origin.task_id != source.ref.task_id)
            or (source.ref.conversation_id and origin.conversation_id != source.ref.conversation_id)
        ):
            return []
        query = query.where(Message.created_at <= origin.created_at)
    rows = await db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(8))
    excerpts: list[HumanStatementExcerpt] = []
    seen: set[str] = set()
    for row in rows:
        payload: Any = row.content_json or {}
        if not isinstance(payload, dict):
            continue
        for key in ("text", "body", "instructions"):
            value = payload.get(key, "")
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_CANDIDATE_CHARS:
                continue
            normalized_value = normalized(value)
            if normalized_value in seen or not _credible_text(value):
                continue
            excerpts.append(HumanStatementExcerpt(value, str(row.id)))
            seen.add(normalized_value)
            if len(excerpts) >= min(limit, 4):
                return excerpts
    return excerpts


async def verified_tool_facts(
    db: AsyncSession, source: SourceFacts, *, limit: int = 8
) -> list[VerifiedToolFact]:
    """Exact, bounded native facts from this task; no synthesized conclusions."""
    if source.internal or not source.ref.task_id or limit < 1:
        return []
    query = (
        select(ToolCall)
        .join(AgentRun, AgentRun.id == ToolCall.run_id)
        .where(
            ToolCall.workspace_id == source.workspace_id,
            AgentRun.workspace_id == source.workspace_id,
            AgentRun.task_id == source.ref.task_id,
            ToolCall.status == "completed",
            ToolCall.tool_name.like("ghost.%"),
        )
    )
    if source.ref.message_id:
        origin = await db.get(Message, source.ref.message_id)
        if (
            origin is None
            or origin.workspace_id != source.workspace_id
            or origin.visibility != "visible"
            or origin.task_id != source.ref.task_id
        ):
            return []
        query = query.where(
            func.coalesce(ToolCall.completed_at, ToolCall.created_at) <= origin.created_at
        )
    rows = await db.scalars(
        query.order_by(ToolCall.created_at.desc(), ToolCall.id.desc()).limit(30)
    )
    facts: list[VerifiedToolFact] = []
    seen: set[str] = set()
    for call in rows:
        output = call.sanitized_output_json or {}
        values = output.get("verified_memory_facts", []) if isinstance(output, dict) else []
        if not isinstance(values, list):
            continue
        for value in values[:20]:
            if not isinstance(value, str) or not 1 <= len(value) <= MAX_CANDIDATE_CHARS:
                continue
            key = normalized(value)
            if key in seen or not _credible_text(value):
                continue
            facts.append(VerifiedToolFact(value, str(call.id)))
            seen.add(key)
            if len(facts) >= min(limit, 600):
                return facts
    return facts


def supported_record(row: MemoryRecord) -> bool:
    policy = row.policy_json or {}
    evidence = policy.get("evidence", {})
    return (
        row.created_by_type == "user"
        or bool(policy.get("approved_by_user"))
        or (isinstance(evidence, dict) and evidence.get("kind") in _SUPPORTED_KINDS)
    )


_SUPPORTED_KINDS = frozenset(
    {"human_statement", "verified_tool_fact", "platform_derived", "approved_editorial_review"}
)


def supported_record_filter() -> ColumnElement[bool]:
    """Filter before candidate limits so unsupported legacy rows cannot crowd out facts."""
    return or_(
        MemoryRecord.created_by_type == "user",
        MemoryRecord.policy_json["approved_by_user"].as_string().is_not(None),
        MemoryRecord.policy_json["evidence"]["kind"].as_string().in_(sorted(_SUPPORTED_KINDS)),
    )


async def candidate_evidence(
    db: AsyncSession, candidate: MemoryCandidate, source: SourceFacts
) -> dict[str, Any] | None:
    text = normalized(candidate.content)
    if not _credible_text(candidate.content):
        return None
    if candidate.source_review_id is not None:
        return await editorial_review_evidence(db, candidate, source)
    query = select(Message).where(
        Message.workspace_id == source.workspace_id,
        Message.sender_type == "user",
        Message.visibility == "visible",
    )
    if candidate.source_message_id is not None:
        query = query.where(Message.id == candidate.source_message_id)
    if source.ref.message_id:
        origin = await db.get(Message, source.ref.message_id)
        if origin is None or origin.workspace_id != source.workspace_id:
            return None
        query = query.where(Message.created_at <= origin.created_at)
    if source.ref.conversation_id:
        query = query.where(Message.conversation_id == source.ref.conversation_id)
    elif source.ref.task_id:
        query = query.where(Message.task_id == source.ref.task_id)
    elif source.ref.message_id:
        query = query.where(Message.id == source.ref.message_id)
    else:
        return None
    rows = await db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(30))
    for row in rows:
        payload = row.content_json or {}
        passages = [payload.get(key, "") for key in ("text", "body", "instructions")]
        if any(isinstance(passage, str) and text in normalized(passage) for passage in passages):
            return {
                "kind": "human_statement",
                "message_id": str(row.id),
                "sha256": hashlib.sha256(candidate.content.encode()).hexdigest(),
            }
    if candidate.source_message_id is not None:
        return None
    for fact in await verified_tool_facts(db, source, limit=600):
        if text == normalized(fact.content):
            return {
                "kind": "verified_tool_fact",
                "tool_call_id": fact.tool_call_id,
                "sha256": hashlib.sha256(candidate.content.encode()).hexdigest(),
            }
    return None


async def editorial_review_evidence(
    db: AsyncSession,
    candidate: MemoryCandidate,
    source: SourceFacts,
) -> dict[str, Any] | None:
    """An exact lesson from a real current approval, shared with its team only."""
    from jhin_db.memberships import active_team_ids
    from jhin_db.models.editorial import (
        EditorialAssignment,
        EditorialReviewPackage,
        GhostEditorialReview,
    )
    from jhin_domain import MemoryScope

    if (
        candidate.capture_class != "editorial_lesson"
        or source.internal
        or candidate.source_message_id is not None
        or source.agent_id is None
        or candidate.requested_scope is not MemoryScope.TEAM
    ):
        return None
    review = await db.scalar(
        select(GhostEditorialReview)
        .where(
            GhostEditorialReview.id == candidate.source_review_id,
            GhostEditorialReview.workspace_id == source.workspace_id,
            GhostEditorialReview.status.in_(("approved", "published")),
        )
        .execution_options(populate_existing=True)
    )
    if (
        review is None
        or review.decided_at is None
        or not review.assignment_id
        or not review.package_id
    ):
        return None
    assignment = await db.get(EditorialAssignment, review.assignment_id, populate_existing=True)
    package = await db.get(EditorialReviewPackage, review.package_id, populate_existing=True)
    if (
        assignment is None
        or package is None
        or assignment.workspace_id != source.workspace_id
        or package.workspace_id != source.workspace_id
        or package.assignment_id != assignment.id
        or assignment.phase == "cancelled"
        or assignment.editorial_version != review.assignment_editorial_version
        or assignment.editorial_version != package.editorial_version
        or assignment.publisher_agent_id != review.publisher_agent_id
        or assignment.writer_agent_id != review.author_agent_id
        or assignment.connection_id != review.connection_id
        or assignment.release_intent != review.release_intent
        or package.manifest_json.get("provider_revision") != review.revision
        or hashlib.sha256(
            json.dumps(
                package.manifest_json, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        != package.revision
        or source.agent_id not in (assignment.writer_agent_id, assignment.publisher_agent_id)
        or candidate.scope_id != assignment.team_id
        or assignment.team_id not in await active_team_ids(db, source.workspace_id, source.agent_id)
        or assignment.team_id
        not in await active_team_ids(db, source.workspace_id, review.publisher_agent_id)
        or not (
            (source.ref.task_id is not None and source.ref.task_id == assignment.task_id)
            or (
                source.ref.conversation_id is not None
                and source.ref.conversation_id == assignment.conversation_id
            )
        )
        or normalized(candidate.content) not in normalized(review.feedback)
    ):
        return None
    from jhin_memory.capture import eligible_capture

    if not eligible_capture(candidate.model_copy(update={"content": review.feedback})):
        return None
    return {
        "kind": "approved_editorial_review",
        "review_id": str(review.id),
        "assignment_id": str(assignment.id),
        "package_id": str(package.id),
        "reviewer_agent_id": str(review.publisher_agent_id),
        "conversation_id": str(assignment.conversation_id) if assignment.conversation_id else None,
        "decided_at": review.decided_at.isoformat(),
        "package_revision": package.revision,
        "sha256": hashlib.sha256(candidate.content.encode()).hexdigest(),
    }
