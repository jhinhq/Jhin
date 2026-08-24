"""Memory management service (experience design, "User controls").

RBAC: any workspace member (viewer+) may read records in the workspace;
members may write agent-scope records; team and workspace scope mutations
and promotion approval require admin. Every mutation is audited without
content; forgetting leaves a content-free tombstone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from opentelemetry.trace import Tracer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.memory.schemas import MemoryCreate, MemoryUpdate
from jhin_db.models import Agent, MemoryRecord, Team
from jhin_domain import (
    ActorType,
    MemoryScope,
    MemoryStatus,
    WorkspaceRole,
    role_satisfies,
)
from jhin_memory import (
    MAX_DEDUP_ADJUDICATED_PAIRS,
    ActorFacts,
    AdjudicationPair,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    compare_contents,
    contains_secret,
    create_version,
    derive_source_facts,
    forget_record,
    resolve_memory_adjudicator,
    resolve_memory_embedder,
)
from jhin_observability import JhinMetrics
from jhin_secrets import SecretCrypto

MAX_PAGE_SIZE = 100
TARGET_TYPE = "memory_record"


@dataclass(frozen=True)
class EmbeddingDeps:
    """What the API needs to build an embedding client: the master-key crypto
    (``None`` when no key is configured) and the process telemetry."""

    crypto: SecretCrypto | None
    metrics: JhinMetrics | None = None
    tracer: Tracer | None = None


async def _embed_best_effort(
    session: AsyncSession,
    ctx: WorkspaceContext,
    deps: EmbeddingDeps | None,
    records: Sequence[MemoryRecord],
    *,
    agent_id: UUID | None,
) -> int:
    """Embed freshly persisted records; failure leaves them without an
    embedding (the embedder logs ``memory.embedding_failed``)."""
    if deps is None or not records:
        return 0
    embedder = await resolve_memory_embedder(
        session,
        deps.crypto,
        workspace_id=ctx.workspace_id,
        agent_id=agent_id,
        metrics=deps.metrics,
        tracer=deps.tracer,
    )
    if embedder is None:
        return 0
    try:
        return await embedder.embed_records(session, records, workspace_id=ctx.workspace_id)
    finally:
        await embedder.close()


def authority_for(role: WorkspaceRole) -> MemoryScope | None:
    """Widest scope a human may activate/mutate directly; None = read-only."""
    if role_satisfies(role, WorkspaceRole.ADMIN):
        return MemoryScope.WORKSPACE
    if role_satisfies(role, WorkspaceRole.MEMBER):
        return MemoryScope.AGENT
    return None


def _require_scope_authority(ctx: WorkspaceContext, scope: MemoryScope) -> None:
    authority = authority_for(ctx.role)
    if authority is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Viewers cannot change memory")
    if scope is not MemoryScope.AGENT and authority is not MemoryScope.WORKSPACE:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"Changing {scope.value} memory requires an admin",
        )


def _actor(ctx: WorkspaceContext, *, explicit: bool = False) -> ActorFacts:
    return ActorFacts(
        actor_type=ActorType.USER,
        actor_id=ctx.user.id,
        explicit=explicit,
        authority=authority_for(ctx.role) or MemoryScope.AGENT,
    )


def _audit(
    session: AsyncSession,
    ctx: WorkspaceContext,
    action: str,
    record_id: UUID,
    *,
    request_id: UUID | None,
    ip_hash: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        action=action,
        target_type=TARGET_TYPE,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        target_id=record_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata=metadata or {},
    )


async def get_memory(session: AsyncSession, workspace_id: UUID, memory_id: UUID) -> MemoryRecord:
    record = await session.scalar(
        select(MemoryRecord).where(
            MemoryRecord.id == memory_id, MemoryRecord.workspace_id == workspace_id
        )
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Memory not found")
    return record


async def list_memories(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    scope: MemoryScope | None = None,
    agent_id: UUID | None = None,
    team_id: UUID | None = None,
    status_filter: MemoryStatus | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MemoryRecord], int]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    query = select(MemoryRecord).where(MemoryRecord.workspace_id == workspace_id)
    if scope is not None:
        query = query.where(MemoryRecord.scope == scope.value)
    if agent_id is not None:
        query = query.where(
            MemoryRecord.scope == MemoryScope.AGENT.value, MemoryRecord.scope_id == agent_id
        )
    if team_id is not None:
        query = query.where(
            MemoryRecord.scope == MemoryScope.TEAM.value, MemoryRecord.scope_id == team_id
        )
    if status_filter is not None:
        query = query.where(MemoryRecord.status == status_filter.value)
    else:
        query = query.where(MemoryRecord.status != MemoryStatus.FORGOTTEN.value)
    if q:
        query = query.where(MemoryRecord.content.ilike(f"%{q.strip()}%"))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(
        query.order_by(
            MemoryRecord.pinned_at.desc().nulls_last(),
            MemoryRecord.created_at.desc(),
            MemoryRecord.id.desc(),
        )
        .limit(limit)
        .offset(max(0, offset))
    )
    return list(rows), total


async def create_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    payload: MemoryCreate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
    embedding: EmbeddingDeps | None = None,
) -> MemoryRecord:
    _require_scope_authority(ctx, payload.scope)
    agent_id = payload.agent_id
    team_id = payload.team_id
    if payload.scope is MemoryScope.AGENT and agent_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="agent_id is required")
    if payload.scope is MemoryScope.TEAM and team_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="team_id is required")
    if agent_id is not None:
        agent = await session.scalar(
            select(Agent).where(Agent.id == agent_id, Agent.workspace_id == ctx.workspace_id)
        )
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")
        if team_id is None:
            team_id = agent.team_id
    if payload.team_id is not None:
        team = await session.scalar(
            select(Team).where(Team.id == payload.team_id, Team.workspace_id == ctx.workspace_id)
        )
        if team is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Team not found")

    source: SourceFacts | None
    if payload.source_conversation_id or payload.source_message_id or payload.source_task_id:
        source = await derive_source_facts(
            session,
            workspace_id=ctx.workspace_id,
            agent_id=agent_id if agent_id is not None else ctx.user.id,
            conversation_id=payload.source_conversation_id,
            message_id=payload.source_message_id,
            task_id=payload.source_task_id,
        )
        if source is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
        source = source.model_copy(update={"agent_id": agent_id, "team_id": team_id})
    else:
        source = SourceFacts(workspace_id=ctx.workspace_id, agent_id=agent_id, team_id=team_id)

    candidate = MemoryCandidate(
        content=payload.content,
        kind=payload.kind,
        subject=payload.subject,
        tags=tuple(payload.tags),
        confidence=payload.confidence,
        importance=payload.importance,
        requested_scope=payload.scope,
        expires_in_days=payload.expires_in_days,
    )
    result = await apply_candidates(
        session, candidates=[candidate], source=source, actor=_actor(ctx, explicit=True)
    )
    decision = result.decisions[0]
    if decision.outcome == "duplicate":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": "This is already remembered",
                "memory_id": str(decision.duplicate_of),
            },
        )
    if decision.outcome == "reject":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Memory rejected by policy", "reasons": list(decision.reasons)},
        )
    record = result.created[0]
    embedded = await _embed_best_effort(session, ctx, embedding, [record], agent_id=agent_id)
    _audit(
        session,
        ctx,
        "memory.created",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "scope": record.scope,
            "status": record.status,
            "embedded": bool(embedded),
            **decision.evidence(),
        },
    )
    await session.commit()
    return record


async def update_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    payload: MemoryUpdate,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
    embedding: EmbeddingDeps | None = None,
) -> MemoryRecord:
    previous = await get_memory(session, ctx.workspace_id, memory_id)
    _require_scope_authority(ctx, MemoryScope(previous.scope))
    if previous.status in (MemoryStatus.FORGOTTEN.value, MemoryStatus.SUPERSEDED.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Cannot edit a {previous.status} memory"
        )
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Nothing to update")
    content = payload.content if payload.content is not None else previous.content
    if contains_secret(content):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Memory rejected by policy", "reasons": ["secret"]},
        )
    new = await create_version(
        session,
        previous,
        content=content,
        actor=_actor(ctx),
        kind=payload.kind.value if payload.kind is not None else None,
        subject=payload.subject if "subject" in values else None,
        tags=payload.tags,
        confidence=payload.confidence,
        importance=payload.importance,
        expires_at=payload.expires_at if "expires_at" in values else None,
    )
    await _embed_best_effort(
        session,
        ctx,
        embedding,
        [new],
        agent_id=new.scope_id if new.scope == MemoryScope.AGENT.value else None,
    )
    _audit(
        session,
        ctx,
        "memory.edited",
        new.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "supersedes_id": str(previous.id),
            "version": new.version,
            "fields": sorted(values.keys()),
        },
    )
    await session.commit()
    return new


async def pin_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    *,
    pinned: bool,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> MemoryRecord:
    record = await get_memory(session, ctx.workspace_id, memory_id)
    _require_scope_authority(ctx, MemoryScope(record.scope))
    if record.status == MemoryStatus.FORGOTTEN.value:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Cannot pin a forgotten memory")
    record.pinned_at = datetime.now(UTC) if pinned else None
    _audit(
        session,
        ctx,
        "memory.pinned" if pinned else "memory.unpinned",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    await session.commit()
    return record


async def contest_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    *,
    reason: str = "",
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> MemoryRecord:
    record = await get_memory(session, ctx.workspace_id, memory_id)
    _require_scope_authority(ctx, MemoryScope(record.scope))
    if record.status not in (MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Cannot contest a {record.status} memory"
        )
    record.status = MemoryStatus.CONTESTED.value
    record.policy_json = {
        **record.policy_json,
        "contested_by_user": str(ctx.user.id),
        "contest_reason": reason[:500],
    }
    _audit(
        session,
        ctx,
        "memory.contested",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"reason": reason[:500]},
    )
    await session.commit()
    return record


async def forget_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> MemoryRecord:
    """Remove live content + embedding for the whole version chain; keep a
    content-free tombstone and audit ``memory.forgotten``."""
    record = await get_memory(session, ctx.workspace_id, memory_id)
    _require_scope_authority(ctx, MemoryScope(record.scope))
    forgotten_ids = await forget_record(session, record)
    _audit(
        session,
        ctx,
        "memory.forgotten",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"forgotten_ids": [str(i) for i in forgotten_ids], "scope": record.scope},
    )
    await session.commit()
    return record


async def approve_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> MemoryRecord:
    """Admin promotion review: proposed → active."""
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Approving memory requires an admin")
    record = await get_memory(session, ctx.workspace_id, memory_id)
    if record.status != MemoryStatus.PROPOSED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Only proposed memory can be approved (is {record.status})",
        )
    record.status = MemoryStatus.ACTIVE.value
    record.valid_from = record.valid_from or datetime.now(UTC)
    record.policy_json = {**record.policy_json, "approved_by_user": str(ctx.user.id)}
    _audit(
        session,
        ctx,
        "memory.approved",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"scope": record.scope},
    )
    await session.commit()
    return record


async def reject_memory(
    session: AsyncSession,
    ctx: WorkspaceContext,
    memory_id: UUID,
    *,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> MemoryRecord:
    """Admin promotion review: proposed → rejected."""
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Rejecting memory requires an admin")
    record = await get_memory(session, ctx.workspace_id, memory_id)
    if record.status != MemoryStatus.PROPOSED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Only proposed memory can be rejected (is {record.status})",
        )
    record.status = MemoryStatus.REJECTED.value
    record.policy_json = {**record.policy_json, "rejected_by_user": str(ctx.user.id)}
    _audit(
        session,
        ctx,
        "memory.rejected",
        record.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"scope": record.scope},
    )
    await session.commit()
    return record


# Bounded scan for the retroactive dedup pass.
MAX_DEDUP_RECORDS = 2_000


def _find_root(parent: list[int], index: int) -> int:
    """Union-find root with path compression."""
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index


async def deduplicate_memories(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    deps: EmbeddingDeps | None = None,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[int, int, int, int, bool]:
    """Admin retroactive cleanup: cluster ACTIVE records per (scope, scope_id)
    with the shared near-duplicate rule (embedding cosine when both sides
    carry a same-model vector, lexical token overlap, subject key), keep the
    best record of each cluster (pinned first, then highest confidence, then
    newest), and mark the rest superseded (content preserved as history).

    When the workspace has a chat-capable default profile (and ``deps`` is
    given), *uncertain* pairs — gray-zone paraphrases the rule cannot match —
    are adjudicated SAME/DIFFERENT by that model, bounded to
    ``MAX_DEDUP_ADJUDICATED_PAIRS`` per call; failure means DIFFERENT, so the
    pass stays idempotent and never merges on doubt.

    Returns ``(clusters_merged, superseded, remaining_active, adjudicated,
    llm)``. Audited as ``memory.deduplicated`` (content-free).
    """
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Deduplicating memory requires an admin"
        )
    rows = list(
        await session.scalars(
            select(MemoryRecord)
            .where(
                MemoryRecord.workspace_id == ctx.workspace_id,
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
                MemoryRecord.content != "",
            )
            .order_by(MemoryRecord.created_at, MemoryRecord.id)
            .limit(MAX_DEDUP_RECORDS)
        )
    )
    groups: dict[tuple[str, UUID], list[MemoryRecord]] = {}
    for row in rows:
        groups.setdefault((row.scope, row.scope_id), []).append(row)
    group_list = [group for group in groups.values() if len(group) >= 2]

    # Pass 1: union rule-detected near duplicates; collect uncertain pairs.
    parents: list[list[int]] = []
    uncertain: list[tuple[int, int, int, float]] = []
    for g_index, group in enumerate(group_list):
        parent = list(range(len(group)))
        parents.append(parent)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                verdict = compare_contents(
                    a.content,
                    b.content,
                    subject_a=a.subject,
                    subject_b=b.subject,
                    embedding_a=a.embedding_json,
                    embedding_b=b.embedding_json,
                    embedding_model_a=a.embedding_model,
                    embedding_model_b=b.embedding_model,
                )
                if verdict.near_duplicate:
                    parent[_find_root(parent, i)] = _find_root(parent, j)
                elif verdict.classification == "uncertain":
                    uncertain.append((g_index, i, j, verdict.score))

    # Pass 2: gray-zone adjudication (best-effort, bounded); SAME unions.
    adjudicated = 0
    llm = False
    if uncertain and deps is not None:
        adjudicator = await resolve_memory_adjudicator(
            session,
            deps.crypto,
            workspace_id=ctx.workspace_id,
            metrics=deps.metrics,
            tracer=deps.tracer,
        )
        if adjudicator is not None:
            llm = True
            try:
                uncertain.sort(key=lambda item: (-item[3], item[0], item[1], item[2]))
                picked: list[tuple[int, int, int]] = []
                for g_index, i, j, _score in uncertain:
                    if len(picked) >= MAX_DEDUP_ADJUDICATED_PAIRS:
                        break
                    if _find_root(parents[g_index], i) == _find_root(parents[g_index], j):
                        continue  # already merged through the rule
                    picked.append((g_index, i, j))
                if picked:
                    pairs = [
                        AdjudicationPair(
                            content_a=group_list[g][i].content,
                            content_b=group_list[g][j].content,
                            subject_a=group_list[g][i].subject,
                            subject_b=group_list[g][j].subject,
                        )
                        for g, i, j in picked
                    ]
                    verdicts = await adjudicator.adjudicate(pairs, workspace_id=ctx.workspace_id)
                    adjudicated = len(pairs)
                    for (g, i, j), same in zip(picked, verdicts, strict=True):
                        if same:
                            parent = parents[g]
                            parent[_find_root(parent, i)] = _find_root(parent, j)
            finally:
                await adjudicator.close()

    now = datetime.now(UTC)
    clusters_merged = 0
    superseded_total = 0
    for g_index, group in enumerate(group_list):
        parent = parents[g_index]
        clusters: dict[int, list[MemoryRecord]] = {}
        for index, row in enumerate(group):
            clusters.setdefault(_find_root(parent, index), []).append(row)
        for members in clusters.values():
            if len(members) < 2:
                continue
            keeper = max(
                members,
                key=lambda r: (r.pinned_at is not None, r.confidence, r.created_at, str(r.id)),
            )
            losers = [m for m in members if m.id != keeper.id]
            merged_tags = list(keeper.tags_json)
            for loser in losers:
                merged_tags.extend(loser.tags_json)
            keeper.tags_json = list(dict.fromkeys(merged_tags))
            keeper.importance = max(m.importance for m in members)
            keeper.policy_json = {
                **keeper.policy_json,
                "confirmations": int(keeper.policy_json.get("confirmations", 0) or 0) + len(losers),
                "last_confirmed": now.isoformat(),
            }
            for loser in losers:
                loser.status = MemoryStatus.SUPERSEDED.value
                loser.policy_json = {
                    **loser.policy_json,
                    "deduplicated_into": str(keeper.id),
                }
            clusters_merged += 1
            superseded_total += len(losers)

    remaining_active = sum(1 for row in rows if row.status == MemoryStatus.ACTIVE.value)
    audit.record(
        session,
        action="memory.deduplicated",
        target_type="workspace",
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        target_id=ctx.workspace_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "scanned": len(rows),
            "clusters": clusters_merged,
            "superseded": superseded_total,
            "remaining_active": remaining_active,
            "adjudicated": adjudicated,
            "llm": llm,
        },
    )
    await session.commit()
    return clusters_merged, superseded_total, remaining_active, adjudicated, llm


async def embed_missing(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    embedding: EmbeddingDeps,
    limit: int,
    request_id: UUID | None = None,
    ip_hash: str | None = None,
) -> tuple[int, int, str, int | None]:
    """Admin backfill: embed up to ``limit`` live records lacking an embedding
    for the workspace's current embedding model. Idempotent — a second call
    embeds nothing. 409 when no enabled profile declares embeddings."""
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Backfilling memory requires an admin"
        )
    embedder = await resolve_memory_embedder(
        session,
        embedding.crypto,
        workspace_id=ctx.workspace_id,
        metrics=embedding.metrics,
        tracer=embedding.tracer,
    )
    if embedder is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "embeddings_unsupported",
                "message": "No enabled model profile in this workspace declares "
                "config_json.embeddings; retrieval stays lexical.",
            },
        )
    try:
        embedded, remaining = await embedder.embed_missing(
            session, workspace_id=ctx.workspace_id, limit=limit
        )
    finally:
        await embedder.close()
    audit.record(
        session,
        action="memory.embed_missing",
        target_type="workspace",
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        target_id=ctx.workspace_id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "embedded": embedded,
            "remaining": remaining,
            "model": embedder.model,
            "limit": limit,
        },
    )
    await session.commit()
    return embedded, remaining, embedder.model, embedder.dimensions
