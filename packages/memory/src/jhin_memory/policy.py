"""Deterministic memory policy (experience design, "Extraction and promotion").

Pure functions, no I/O. Given a candidate, immutable source facts, the actor,
and the existing records in the target scope, :func:`evaluate_candidate`
decides *reject / duplicate / propose / activate* and the resulting status.

Rules:

- secrets are rejected or redacted before anything else (``screening``);
- INTERNAL / hidden sources are never memory;
- **non-amplification**: the requested scope may never exceed the source's
  visibility ceiling — except for an explicit human "remember this" whose
  RBAC authority covers the scope (a human is the authority over their own
  statement);
- exact duplicates (same normalized-content hash in the same scope) are not
  stored again;
- a different value for the same ``subject`` in the same scope marks both
  records ``contested``;
- agent-private memory activates automatically; team memory activates when
  the source was already team-visible; workspace promotion always stays
  ``proposed`` until an admin approves it.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Collection, Sequence
from uuid import UUID

from jhin_domain import (
    MEMORY_SCOPE_ORDER,
    ActorType,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
)
from jhin_memory.screening import is_low_information, is_self_referential, screen_content
from jhin_memory.similarity import SimilarityVerdict, compare_contents, token_set
from jhin_memory.types import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    MemoryDecision,
    SourceFacts,
)

# A near-duplicate candidate must beat the stored record's confidence by this
# margin (or add new information at equal confidence) to become a new version;
# otherwise it is skipped and the stored record is confirmed instead.
NEAR_DUPLICATE_CONFIDENCE_MARGIN = 0.1

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[.!?;:,\s]+$")

# Statuses that count as "already stored" for dedup / contradiction.
_LIVE_STATUSES = frozenset({MemoryStatus.ACTIVE, MemoryStatus.CONTESTED, MemoryStatus.PROPOSED})


def normalize_content(content: str) -> str:
    """Case/whitespace/punctuation-insensitive canonical form for hashing."""
    text = unicodedata.normalize("NFKC", content).casefold()
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _TRAILING_PUNCT_RE.sub("", text)
    return text


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def normalize_subject(subject: str | None) -> str | None:
    if subject is None:
        return None
    text = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", subject).casefold()).strip()
    return text[:200] or None


def scope_exceeds(requested: MemoryScope, ceiling: MemoryScope) -> bool:
    return MEMORY_SCOPE_ORDER[requested] > MEMORY_SCOPE_ORDER[ceiling]


def resolve_scope_id(scope: MemoryScope, source: SourceFacts) -> UUID | None:
    if scope is MemoryScope.AGENT:
        return source.agent_id
    if scope is MemoryScope.TEAM:
        return source.team_id
    return source.workspace_id


def evaluate_candidate(
    candidate: MemoryCandidate,
    source: SourceFacts,
    actor: ActorFacts,
    existing: Sequence[ExistingRecord] = (),
    *,
    candidate_embedding: Sequence[float] | None = None,
    embedding_model: str | None = None,
    agent_name: str = "",
    adjudicated_same: Collection[UUID] = frozenset(),
) -> MemoryDecision:
    reasons: list[str] = []
    human_explicit = actor.explicit and actor.actor_type is ActorType.USER

    # 1. Secret screening.
    screened = screen_content(candidate.content)
    reasons.extend(screened.reasons)
    if screened.rejected:
        return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))
    sensitivity = MemorySensitivity.REDACTED if screened.redacted else MemorySensitivity.NORMAL

    # 1b. Deterministic quality screening for model-proposed candidates: the
    # agent memorising its own identity, and near-empty content, are never
    # memory. An explicit human "remember this" bypasses (their statement,
    # their call) — unless the person only authorised the *scope* and a model
    # wrote the words, in which case nobody has vouched for the wording.
    if not human_explicit or actor.authored_by_model:
        if is_self_referential(screened.content, agent_name):
            reasons.append("self_reference")
            return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))
        if is_low_information(screened.content):
            reasons.append("low_information")
            return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))

    # 2. Hidden sources are never memory.
    if source.internal:
        reasons.append("source_internal")
        return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))

    # 3. Scope ceiling / non-amplification.
    scope = candidate.requested_scope
    if human_explicit:
        if scope_exceeds(scope, actor.authority):
            reasons.append("insufficient_authority")
            return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))
        visibility = scope
    else:
        if scope_exceeds(scope, source.visibility):
            reasons.append("non_amplification")
            return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))
        visibility = source.visibility

    scope_id = resolve_scope_id(scope, source)
    if scope_id is None:
        reasons.append("no_team_for_scope" if scope is MemoryScope.TEAM else "no_agent_for_scope")
        return MemoryDecision(candidate=candidate, outcome="reject", reasons=tuple(reasons))

    # 4. Normalization + dedup.
    digest = content_hash(screened.content)
    subject = normalize_subject(candidate.subject)
    in_scope = [
        record
        for record in existing
        if record.scope is scope and record.scope_id == scope_id and record.status in _LIVE_STATUSES
    ]
    duplicate = next((record for record in in_scope if record.content_hash == digest), None)
    if duplicate is not None:
        reasons.append("duplicate")
        return MemoryDecision(
            candidate=candidate,
            outcome="duplicate",
            reasons=tuple(reasons),
            duplicate_of=duplicate.id,
            content=screened.content,
            content_hash=digest,
            scope=scope,
            scope_id=scope_id,
            visibility=visibility,
            sensitivity=sensitivity,
        )

    # 4b. Semantic near-duplicate: the same fact in different words (or a
    # better wording/value of a keyed fact). Never two active near-duplicates
    # in one scope: the weaker one is skipped (and the stored record
    # confirmed), a meaningfully better one becomes the next VERSION.
    # ``adjudicated_same`` carries record ids an LLM adjudication pass judged
    # to state the same fact (jhin_memory.adjudication); they are treated
    # exactly like rule-detected near duplicates.
    best: tuple[SimilarityVerdict, ExistingRecord] | None = None
    for record in in_scope:
        if not record.content:
            continue
        verdict = compare_contents(
            screened.content,
            record.content,
            subject_a=subject,
            subject_b=record.subject,
            embedding_a=candidate_embedding,
            embedding_b=record.embedding,
            embedding_model_a=embedding_model,
            embedding_model_b=record.embedding_model,
        )
        if (verdict.near_duplicate or record.id in adjudicated_same) and (
            best is None or verdict.score > best[0].score
        ):
            best = (verdict, record)

    supersedes: UUID | None = None
    if best is not None:
        near = best[1]
        if not best[0].near_duplicate:
            reasons.append("adjudicated_same")
        candidate_tokens = token_set(screened.content)
        near_tokens = token_set(near.content)
        more_specific = bool(candidate_tokens - near_tokens) and len(candidate_tokens) > len(
            near_tokens
        )
        better = candidate.confidence >= near.confidence + NEAR_DUPLICATE_CONFIDENCE_MARGIN or (
            candidate.confidence >= near.confidence and more_specific
        )
        if not better:
            reasons.append("near_duplicate")
            return MemoryDecision(
                candidate=candidate,
                outcome="duplicate",
                reasons=tuple(reasons),
                duplicate_of=near.id,
                content=screened.content,
                content_hash=digest,
                scope=scope,
                scope_id=scope_id,
                visibility=visibility,
                sensitivity=sensitivity,
            )
        supersedes = near.id
        reasons.append("near_duplicate_superseded")

    # 5. Contradiction: same subject, different value (a record being
    # superseded by this candidate is its previous version, not a conflict).
    contested_with = tuple(
        record.id
        for record in in_scope
        if subject is not None
        and record.subject == subject
        and record.content_hash != digest
        and record.id != supersedes
        and record.status in (MemoryStatus.ACTIVE, MemoryStatus.CONTESTED)
    )

    # 6. Promotion / activation.
    if human_explicit:
        status = MemoryStatus.ACTIVE
        reasons.append("explicit_remember")
        outcome: str = "activate"
    elif scope is MemoryScope.WORKSPACE:
        status = MemoryStatus.PROPOSED
        reasons.append("workspace_promotion_requires_review")
        outcome = "propose"
    elif scope is MemoryScope.TEAM:
        status = MemoryStatus.ACTIVE
        reasons.append("team_source_visible")
        outcome = "activate"
    else:
        status = MemoryStatus.ACTIVE
        reasons.append("agent_private_auto_activate")
        outcome = "activate"

    if contested_with and status is MemoryStatus.ACTIVE:
        status = MemoryStatus.CONTESTED
        reasons.append("contradiction")

    return MemoryDecision(
        candidate=candidate,
        outcome="activate" if outcome == "activate" else "propose",
        reasons=tuple(reasons),
        status=status,
        scope=scope,
        scope_id=scope_id,
        visibility=visibility,
        sensitivity=sensitivity,
        content=screened.content,
        content_hash=digest,
        contested_with=contested_with,
        supersedes=supersedes,
    )
