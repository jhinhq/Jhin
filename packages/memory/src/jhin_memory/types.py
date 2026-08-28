"""Provider-neutral contracts for curated memory.

Everything here is pure data. The model may *propose* a
:class:`MemoryCandidate`; deterministic code (``jhin_memory.policy``) turns it
into a :class:`MemoryDecision`. Activation, scope, and visibility are never
model decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jhin_domain import ActorType, MemoryKind, MemoryScope, MemorySensitivity, MemoryStatus

MAX_CANDIDATE_CHARS = 2_000
MAX_CANDIDATES_PER_EXTRACTION = 20
MAX_TAGS = 10


class MemoryCandidate(BaseModel):
    """One proposed memory. Strict: unknown keys are rejected (model output is
    validated, never trusted)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    kind: MemoryKind = MemoryKind.OTHER
    # Normalized subject key for contradiction detection, e.g. "deploy.day".
    subject: str | None = Field(default=None, max_length=200)
    tags: tuple[str, ...] = ()
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    requested_scope: MemoryScope = MemoryScope.AGENT
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags")
        cleaned = tuple(tag.strip().lower()[:50] for tag in value if tag.strip())
        return tuple(dict.fromkeys(cleaned))

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must not be blank")
        return stripped


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: UUID | None = None
    message_id: UUID | None = None
    task_id: UUID | None = None
    event_id: UUID | None = None


class SourceFacts(BaseModel):
    """Immutable facts about where a candidate came from, computed by code
    (``jhin_memory.persistence.derive_source_facts``), never by the model."""

    model_config = ConfigDict(frozen=True)

    workspace_id: UUID
    # None for explicit team/workspace memory a human records without an agent.
    agent_id: UUID | None = None
    # The source's visibility ceiling: a memory can never be broader.
    visibility: MemoryScope = MemoryScope.AGENT
    team_id: UUID | None = None
    # Hidden reasoning / INTERNAL messages are never memory.
    internal: bool = False
    ref: SourceRef = SourceRef()


class ActorFacts(BaseModel):
    """Who is proposing. ``explicit`` is an authenticated human "remember
    this" instruction; ``authority`` is the widest scope that actor may
    activate directly (from workspace RBAC), irrelevant for non-explicit
    proposals."""

    model_config = ConfigDict(frozen=True)

    actor_type: ActorType
    actor_id: UUID | None = None
    explicit: bool = False
    authority: MemoryScope = MemoryScope.AGENT
    # True when a person authorised the *scope* but a model wrote the
    # *words* (an agent's memory.propose citing an answered question). The
    # explicit path still bypasses non-amplification — that is the point —
    # but the quality screens stay on, because the person never vouched for
    # the wording.
    authored_by_model: bool = False


class ScreeningResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    rejected: bool = False
    redacted: bool = False
    reasons: tuple[str, ...] = ()


Outcome = Literal["activate", "propose", "reject", "duplicate"]


class ExistingRecord(BaseModel):
    """The slice of a stored record the policy needs.

    ``content`` powers lexical near-duplicate detection; ``embedding`` /
    ``embedding_model`` power semantic near-duplicate detection (compared
    only against a candidate vector from the same model)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    scope: MemoryScope
    scope_id: UUID
    status: MemoryStatus
    content_hash: str
    subject: str | None = None
    content: str = ""
    confidence: float = 0.5
    importance: float = 0.5
    version: int = 1
    tags: tuple[str, ...] = ()
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None


class MemoryDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: MemoryCandidate
    outcome: Outcome
    reasons: tuple[str, ...] = ()
    # Fields below are set when outcome is activate/propose.
    status: MemoryStatus | None = None
    scope: MemoryScope | None = None
    scope_id: UUID | None = None
    visibility: MemoryScope | None = None
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    content: str = ""
    content_hash: str = ""
    duplicate_of: UUID | None = None
    contested_with: tuple[UUID, ...] = ()
    # Near-duplicate upgrade: the new record is a better wording/value of an
    # existing one and must be persisted as its next VERSION (the previous
    # record becomes ``superseded``) — never two active near-duplicates.
    supersedes: UUID | None = None

    def evidence(self) -> dict[str, Any]:
        """Content-free policy evidence for ``policy_json`` / audit."""
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "requested_scope": self.candidate.requested_scope.value,
            "sensitivity": self.sensitivity.value,
            "duplicate_of": str(self.duplicate_of) if self.duplicate_of else None,
            "contested_with": [str(i) for i in self.contested_with],
            "supersedes": str(self.supersedes) if self.supersedes else None,
        }


RetrievalMode = Literal["hybrid", "lexical", "unavailable"]


class MemoryContextItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    version: int
    kind: MemoryKind
    scope: MemoryScope
    status: MemoryStatus
    content: str
    source_label: str
    score: float
    pinned: bool = False


class MemoryProvenance(BaseModel):
    """What was selected and why — never the content. Logged as the
    ``memory.retrieved`` run event."""

    model_config = ConfigDict(frozen=True)

    record_ids: tuple[UUID, ...] = ()
    versions: tuple[int, ...] = ()
    mode: RetrievalMode = "unavailable"
    degraded: bool = False
    policy: dict[str, Any] = Field(default_factory=dict)
    context_hash: str = ""
    query_hash: str = ""
    max_records: int = 0
    max_chars: int = 0
    retrieved_at: datetime | None = None

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "record_ids": [str(i) for i in self.record_ids],
            "versions": list(self.versions),
            "mode": self.mode,
            "degraded": self.degraded,
            "policy": self.policy,
            "context_hash": self.context_hash,
            "query_hash": self.query_hash,
            "max_records": self.max_records,
            "max_chars": self.max_chars,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
        }


class MemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Rendered, bounded text the worker appends to the system prompt ("" when
    # nothing was selected).
    text: str
    items: tuple[MemoryContextItem, ...]
    provenance: MemoryProvenance
