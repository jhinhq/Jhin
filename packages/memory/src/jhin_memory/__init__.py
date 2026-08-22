"""jhin-memory: curated long-term memory for agents.

Deterministic policy (:mod:`jhin_memory.policy`), persistence
(:mod:`jhin_memory.persistence`), hybrid retrieval with run provenance
(:mod:`jhin_memory.retrieval`), and the structured extraction contract
(:mod:`jhin_memory.extraction`). See docs/architecture/memory.md.
"""

from jhin_memory.embedding import (
    DEFAULT_BACKFILL_LIMIT,
    MAX_BACKFILL_LIMIT,
    MEMORY_EMBEDDED_EVENT,
    MEMORY_EMBEDDING_FAILED_EVENT,
    EmbeddingProfile,
    MemoryEmbedder,
    resolve_memory_embedder,
    select_embedding_profile,
)
from jhin_memory.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    CandidateParseError,
    ExtractionResult,
    build_extraction_request,
    extract_candidates,
    parse_candidates,
)
from jhin_memory.persistence import (
    ApplyResult,
    agent_team_ids,
    apply_candidates,
    create_version,
    derive_source_facts,
    forget_record,
    set_embedding,
    system_actor,
)
from jhin_memory.policy import (
    content_hash,
    evaluate_candidate,
    normalize_content,
    normalize_subject,
    scope_exceeds,
)
from jhin_memory.retrieval import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_RECORDS,
    MEMORY_RETRIEVED_EVENT,
    authorization_filter,
    build_memory_context,
    record_retrieval_provenance,
    render_context,
    unavailable_context,
)
from jhin_memory.screening import REDACTION_MARKER, contains_secret, screen_content
from jhin_memory.types import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    MemoryContext,
    MemoryContextItem,
    MemoryDecision,
    MemoryProvenance,
    ScreeningResult,
    SourceFacts,
    SourceRef,
)

__all__ = [
    "DEFAULT_BACKFILL_LIMIT",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_RECORDS",
    "EXTRACTION_SYSTEM_PROMPT",
    "MAX_BACKFILL_LIMIT",
    "MEMORY_EMBEDDED_EVENT",
    "MEMORY_EMBEDDING_FAILED_EVENT",
    "MEMORY_RETRIEVED_EVENT",
    "REDACTION_MARKER",
    "ActorFacts",
    "ApplyResult",
    "CandidateParseError",
    "EmbeddingProfile",
    "ExistingRecord",
    "ExtractionResult",
    "MemoryCandidate",
    "MemoryContext",
    "MemoryContextItem",
    "MemoryDecision",
    "MemoryEmbedder",
    "MemoryProvenance",
    "ScreeningResult",
    "SourceFacts",
    "SourceRef",
    "agent_team_ids",
    "apply_candidates",
    "authorization_filter",
    "build_extraction_request",
    "build_memory_context",
    "contains_secret",
    "content_hash",
    "create_version",
    "derive_source_facts",
    "evaluate_candidate",
    "extract_candidates",
    "forget_record",
    "normalize_content",
    "normalize_subject",
    "parse_candidates",
    "record_retrieval_provenance",
    "render_context",
    "resolve_memory_embedder",
    "scope_exceeds",
    "screen_content",
    "select_embedding_profile",
    "set_embedding",
    "system_actor",
    "unavailable_context",
]
