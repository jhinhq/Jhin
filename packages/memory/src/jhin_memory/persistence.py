"""Memory persistence: apply policy decisions, version edits, forget.

All functions stage changes on the caller's :class:`AsyncSession` and flush;
the caller owns the transaction. Nothing here decides policy — that is
:mod:`jhin_memory.policy`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentTeamMembership, Conversation, MemoryRecord, Message, Task
from jhin_domain import (
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MessageVisibility,
)
from jhin_memory.policy import content_hash, evaluate_candidate, normalize_subject
from jhin_memory.types import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    MemoryDecision,
    SourceFacts,
    SourceRef,
)
from jhin_memory.vector import store_embedding_vector

# Rows considered when deduplicating / detecting contradictions in one scope.
_EXISTING_LIMIT = 500
_LIVE_STATUSES = (
    MemoryStatus.ACTIVE.value,
    MemoryStatus.CONTESTED.value,
    MemoryStatus.PROPOSED.value,
)


@dataclass
class ApplyResult:
    created: list[MemoryRecord] = field(default_factory=list)
    decisions: list[MemoryDecision] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return sum(1 for d in self.decisions if d.outcome == "reject")

    @property
    def duplicates(self) -> int:
        return sum(1 for d in self.decisions if d.outcome == "duplicate")

    @property
    def confirmed(self) -> int:
        """Near-duplicates that bumped an existing record instead of storing."""
        return sum(
            1 for d in self.decisions if d.outcome == "duplicate" and "near_duplicate" in d.reasons
        )

    @property
    def superseded(self) -> int:
        """Created records that replaced an existing near-duplicate version."""
        return sum(1 for r in self.created if r.supersedes_id is not None)

    def summary(self) -> dict[str, Any]:
        """Content-free summary for run events / workflow results."""
        return {
            "created": [str(r.id) for r in self.created],
            "activated": sum(1 for r in self.created if r.status == MemoryStatus.ACTIVE.value),
            "proposed": sum(1 for r in self.created if r.status == MemoryStatus.PROPOSED.value),
            "contested": sum(1 for r in self.created if r.status == MemoryStatus.CONTESTED.value),
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "confirmed": self.confirmed,
            "superseded": self.superseded,
            "reasons": sorted({reason for d in self.decisions for reason in d.reasons}),
        }


async def agent_team_ids(session: AsyncSession, workspace_id: UUID, agent_id: UUID) -> list[UUID]:
    """Every team the agent belongs to: primary team plus memberships."""
    ids: list[UUID] = []
    primary = await session.scalar(
        select(Agent.team_id).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
    )
    if primary is not None:
        ids.append(primary)
    rows = await session.scalars(
        select(AgentTeamMembership.team_id).where(
            AgentTeamMembership.workspace_id == workspace_id,
            AgentTeamMembership.agent_id == agent_id,
        )
    )
    for team_id in rows:
        if team_id not in ids:
            ids.append(team_id)
    return ids


async def derive_source_facts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    task_id: UUID | None = None,
    event_id: UUID | None = None,
) -> SourceFacts | None:
    """Compute the immutable visibility facts of a source. ``None`` when the
    source does not exist in this workspace (the candidate must be dropped).

    Visibility ceiling rules (deterministic, documented in
    docs/architecture/memory.md):

    - an INTERNAL message is hidden → ``internal=True`` (never memory);
    - a task with ``assigned_team_id`` is team-visible (ceiling ``team``);
    - everything else (a chat with one agent, an unassigned task) is
      agent-private (ceiling ``agent``).
    """
    internal = False
    team_id: UUID | None = None
    visibility = MemoryScope.AGENT

    if message_id is not None:
        message = await session.scalar(
            select(Message).where(Message.id == message_id, Message.workspace_id == workspace_id)
        )
        if message is None:
            return None
        if message.visibility == MessageVisibility.INTERNAL.value:
            internal = True
        if task_id is None:
            task_id = message.task_id
        if conversation_id is None:
            conversation_id = message.conversation_id

    if task_id is not None:
        task = await session.scalar(
            select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
        )
        if task is None:
            return None
        if task.assigned_team_id is not None:
            team_id = task.assigned_team_id
            visibility = MemoryScope.TEAM
        else:
            # A delegated / work-request task between two agents of the SAME
            # team was visible to that team's lineage: its exchange may
            # activate team memory (never broader). Different teams (or no
            # counterpart) keep the agent-private ceiling.
            shared = await _shared_team_for_task(session, workspace_id, task)
            if shared is not None:
                team_id = shared
                visibility = MemoryScope.TEAM
        if conversation_id is None:
            conversation_id = task.conversation_id

    if conversation_id is not None:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
            )
        )
        if conversation is None:
            return None

    if team_id is None:
        # Team-scoped proposals still need *a* team to land in; the agent's
        # primary team is the only deterministic choice. Non-amplification is
        # enforced separately through ``visibility``.
        team_id = await session.scalar(
            select(Agent.team_id).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        )

    return SourceFacts(
        workspace_id=workspace_id,
        agent_id=agent_id,
        visibility=visibility,
        team_id=team_id,
        internal=internal,
        ref=SourceRef(
            conversation_id=conversation_id,
            message_id=message_id,
            task_id=task_id,
            event_id=event_id,
        ),
    )


async def _shared_team_for_task(
    session: AsyncSession, workspace_id: UUID, task: Task
) -> UUID | None:
    """The primary team both sides of an agent↔agent task belong to, when the
    task came from a delegation or an accepted work request; ``None``
    otherwise. Deterministic and conservative: only the two named agents'
    primary teams are compared."""
    counterpart_raw: str = ""
    delegation = task.metadata_json.get("delegation") if task.metadata_json else None
    if isinstance(delegation, dict):
        counterpart_raw = str(delegation.get("delegated_by_agent_id", "") or "")
    else:
        work_request = task.metadata_json.get("work_request") if task.metadata_json else None
        if isinstance(work_request, dict):
            counterpart_raw = str(work_request.get("requester_agent_id", "") or "")
    if not counterpart_raw or task.assigned_agent_id is None:
        return None
    try:
        counterpart_id = UUID(counterpart_raw)
    except ValueError:
        return None
    assigned_team = await session.scalar(
        select(Agent.team_id).where(
            Agent.id == task.assigned_agent_id, Agent.workspace_id == workspace_id
        )
    )
    counterpart_team = await session.scalar(
        select(Agent.team_id).where(Agent.id == counterpart_id, Agent.workspace_id == workspace_id)
    )
    if assigned_team is not None and assigned_team == counterpart_team:
        return assigned_team
    return None


async def _existing_in_scope(
    session: AsyncSession, workspace_id: UUID, scope: MemoryScope, scope_id: UUID
) -> list[MemoryRecord]:
    rows = await session.scalars(
        select(MemoryRecord)
        .where(
            MemoryRecord.workspace_id == workspace_id,
            MemoryRecord.scope == scope.value,
            MemoryRecord.scope_id == scope_id,
            MemoryRecord.status.in_(_LIVE_STATUSES),
        )
        .order_by(MemoryRecord.created_at.desc())
        .limit(_EXISTING_LIMIT)
    )
    return list(rows)


def _as_existing(record: MemoryRecord) -> ExistingRecord:
    return ExistingRecord(
        id=record.id,
        scope=MemoryScope(record.scope),
        scope_id=record.scope_id,
        status=MemoryStatus(record.status),
        content_hash=record.content_hash,
        subject=record.subject,
        content=record.content,
        confidence=record.confidence,
        importance=record.importance,
        version=record.version,
        tags=tuple(record.tags_json),
        embedding=tuple(record.embedding_json) if record.embedding_json else None,
        embedding_model=record.embedding_model,
    )


async def apply_candidates(
    session: AsyncSession,
    *,
    candidates: Sequence[MemoryCandidate],
    source: SourceFacts,
    actor: ActorFacts,
    now: datetime | None = None,
    candidate_embeddings: Sequence[Sequence[float] | None] | None = None,
    embedding_model: str | None = None,
    agent_name: str = "",
) -> ApplyResult:
    """Run policy over ``candidates`` and persist the survivors.

    ``candidate_embeddings`` (parallel to ``candidates``, best-effort) powers
    semantic near-duplicate detection and is attached to created records so
    they need no second embedding call. A near-duplicate that adds nothing
    bumps the stored record (confirmation count, importance, tag union); a
    meaningfully better one becomes the next version of the stored record.
    """
    now = now or datetime.now(UTC)
    result = ApplyResult()
    cache: dict[tuple[str, UUID], list[MemoryRecord]] = {}
    pending_vectors: list[tuple[MemoryRecord, Sequence[float]]] = []

    for index, candidate in enumerate(candidates):
        vector: Sequence[float] | None = None
        if candidate_embeddings is not None and index < len(candidate_embeddings):
            vector = candidate_embeddings[index]

        # Pre-resolve the target scope so the existing set can be loaded;
        # the policy re-derives it and may still reject.
        scope = candidate.requested_scope
        scope_id = (
            source.agent_id
            if scope is MemoryScope.AGENT
            else source.team_id
            if scope is MemoryScope.TEAM
            else source.workspace_id
        )
        existing_rows: list[MemoryRecord] = []
        if scope_id is not None:
            key = (scope.value, scope_id)
            if key not in cache:
                cache[key] = await _existing_in_scope(session, source.workspace_id, scope, scope_id)
            existing_rows = cache[key]

        decision = evaluate_candidate(
            candidate,
            source,
            actor,
            [_as_existing(r) for r in existing_rows],
            candidate_embedding=vector,
            embedding_model=embedding_model,
            agent_name=agent_name,
        )
        result.decisions.append(decision)
        if decision.outcome == "duplicate" and decision.duplicate_of is not None:
            # Confirmation: the same fact came up again. Strengthen the
            # stored record instead of storing a wording variant.
            for row in existing_rows:
                if row.id == decision.duplicate_of:
                    row.policy_json = {
                        **row.policy_json,
                        "confirmations": int(row.policy_json.get("confirmations", 0) or 0) + 1,
                        "last_confirmed": now.isoformat(),
                    }
                    row.importance = max(row.importance, candidate.importance)
                    if candidate.tags:
                        row.tags_json = list(dict.fromkeys([*row.tags_json, *candidate.tags]))
                    break
            continue
        if decision.outcome not in ("activate", "propose"):
            continue
        assert decision.status is not None
        assert decision.scope is not None
        assert decision.scope_id is not None
        assert decision.visibility is not None

        for contested_id in decision.contested_with:
            for row in existing_rows:
                if row.id == contested_id and row.status == MemoryStatus.ACTIVE.value:
                    row.status = MemoryStatus.CONTESTED.value
                    row.policy_json = {**row.policy_json, "contested_by": "new_version_pending"}

        previous: MemoryRecord | None = None
        if decision.supersedes is not None:
            previous = next((r for r in existing_rows if r.id == decision.supersedes), None)

        tags = list(candidate.tags)
        if previous is not None:
            tags = list(dict.fromkeys([*previous.tags_json, *tags]))

        record = MemoryRecord(
            workspace_id=source.workspace_id,
            scope=decision.scope.value,
            scope_id=decision.scope_id,
            kind=candidate.kind.value,
            subject=normalize_subject(candidate.subject)
            or (previous.subject if previous is not None else None),
            content=decision.content,
            content_hash=decision.content_hash,
            source_conversation_id=source.ref.conversation_id,
            source_message_id=source.ref.message_id,
            source_task_id=source.ref.task_id,
            source_event_id=source.ref.event_id,
            visibility=decision.visibility.value,
            sensitivity=decision.sensitivity.value,
            confidence=candidate.confidence,
            importance=candidate.importance
            if previous is None
            else max(candidate.importance, previous.importance),
            tags_json=tags,
            status=decision.status.value,
            valid_from=now,
            expires_at=(
                now + timedelta(days=candidate.expires_in_days)
                if candidate.expires_in_days
                else None
            ),
            pinned_at=previous.pinned_at if previous is not None else None,
            version=previous.version + 1 if previous is not None else 1,
            supersedes_id=previous.id if previous is not None else None,
            created_by_type=actor.actor_type.value,
            created_by_id=actor.actor_id,
            policy_json=decision.evidence(),
        )
        if previous is not None:
            previous.status = MemoryStatus.SUPERSEDED.value
            existing_rows.remove(previous)
        if vector is not None:
            record.embedding_json = [float(v) for v in vector]
            record.embedding_model = embedding_model
            record.embedding_dimensions = len(vector)
            pending_vectors.append((record, vector))
        session.add(record)
        existing_rows.append(record)
        result.created.append(record)

    await session.flush()
    for record, vector in pending_vectors:
        await store_embedding_vector(session, record.id, [float(v) for v in vector])
    return result


async def set_embedding(
    session: AsyncSession,
    record: MemoryRecord,
    vector: Sequence[float],
    *,
    model: str,
) -> None:
    """Attach an embedding (portable JSON + pgvector column when available)."""
    record.embedding_json = [float(v) for v in vector]
    record.embedding_model = model
    record.embedding_dimensions = len(vector)
    await session.flush()
    await store_embedding_vector(session, record.id, record.embedding_json)


async def create_version(
    session: AsyncSession,
    previous: MemoryRecord,
    *,
    content: str,
    actor: ActorFacts,
    kind: str | None = None,
    subject: str | None = None,
    tags: Sequence[str] | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> MemoryRecord:
    """Edit = new immutable version; the previous one becomes superseded."""
    now = now or datetime.now(UTC)
    new = MemoryRecord(
        workspace_id=previous.workspace_id,
        scope=previous.scope,
        scope_id=previous.scope_id,
        kind=kind or previous.kind,
        subject=normalize_subject(subject) if subject is not None else previous.subject,
        content=content,
        content_hash=content_hash(content),
        source_conversation_id=previous.source_conversation_id,
        source_message_id=previous.source_message_id,
        source_task_id=previous.source_task_id,
        source_event_id=previous.source_event_id,
        visibility=previous.visibility,
        sensitivity=previous.sensitivity,
        confidence=previous.confidence if confidence is None else confidence,
        importance=previous.importance if importance is None else importance,
        tags_json=list(tags) if tags is not None else list(previous.tags_json),
        status=(
            MemoryStatus.PROPOSED.value
            if previous.status == MemoryStatus.PROPOSED.value
            else MemoryStatus.ACTIVE.value
        ),
        valid_from=now,
        expires_at=expires_at if expires_at is not None else previous.expires_at,
        pinned_at=previous.pinned_at,
        version=previous.version + 1,
        supersedes_id=previous.id,
        created_by_type=actor.actor_type.value,
        created_by_id=actor.actor_id,
        policy_json={"outcome": "edit", "reasons": ["edited"], "edited_from": str(previous.id)},
    )
    previous.status = MemoryStatus.SUPERSEDED.value
    session.add(new)
    await session.flush()
    return new


async def forget_record(
    session: AsyncSession, record: MemoryRecord, *, now: datetime | None = None
) -> list[UUID]:
    """Remove live content and embeddings from the record *and every earlier
    version in its chain*, leaving content-free tombstones. Returns the ids
    that were tombstoned."""
    now = now or datetime.now(UTC)
    forgotten: list[UUID] = []
    current: MemoryRecord | None = record
    seen: set[UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        current.content = ""
        current.content_hash = ""
        current.subject = None
        current.tags_json = []
        current.embedding_json = None
        current.embedding_model = None
        current.embedding_dimensions = None
        current.status = MemoryStatus.FORGOTTEN.value
        current.forgotten_at = now
        current.pinned_at = None
        current.sensitivity = MemorySensitivity.NORMAL.value
        current.policy_json = {"outcome": "forgotten"}
        forgotten.append(current.id)
        await store_embedding_vector(session, current.id, None)
        current = (
            await session.get(MemoryRecord, current.supersedes_id)
            if current.supersedes_id is not None
            else None
        )
    await session.flush()
    return forgotten
