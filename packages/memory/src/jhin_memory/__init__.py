"""jhin-memory: curated long-term memory for agents.

Deterministic policy (:mod:`jhin_memory.policy`), persistence
(:mod:`jhin_memory.persistence`), hybrid retrieval with run provenance
(:mod:`jhin_memory.retrieval`), and the structured extraction contract
(:mod:`jhin_memory.extraction`). See docs/architecture/memory.md.
"""

from jhin_memory.adjudication import (
    ADJUDICATION_SYSTEM_PROMPT,
    MAX_APPLY_ADJUDICATED_PAIRS,
    MAX_DEDUP_ADJUDICATED_PAIRS,
    AdjudicationPair,
    AdjudicationParseError,
    MemoryAdjudicator,
    PairAdjudicator,
    build_adjudication_request,
    parse_adjudication,
    resolve_memory_adjudicator,
)
from jhin_memory.embedding import (
    DEFAULT_BACKFILL_LIMIT,
    MAX_BACKFILL_LIMIT,
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
    agent_team_ids,
    apply_candidates,
    create_version,
    derive_source_facts,
    forget_record,
    set_embedding,
)
from jhin_memory.policy import (
    content_hash,
    evaluate_candidate,
    normalize_content,
)
from jhin_memory.retrieval import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_RECORDS,
    MEMORY_RETRIEVED_EVENT,
    authorization_filter,
    build_memory_context,
    record_retrieval_provenance,
    unavailable_context,
)
from jhin_memory.screening import (
    contains_secret,
    is_low_information,
    is_self_referential,
    screen_content,
)
from jhin_memory.similarity import (
    LEXICAL_DUPLICATE_JACCARD,
    SEMANTIC_DUPLICATE_COSINE,
    SimilarityVerdict,
    compare_contents,
    token_set,
)
from jhin_memory.types import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    MemoryContext,
    MemoryContextItem,
    MemoryDecision,
    MemoryProvenance,
    SourceFacts,
    SourceRef,
)

__all__ = [
    "ADJUDICATION_SYSTEM_PROMPT",
    "DEFAULT_BACKFILL_LIMIT",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_RECORDS",
    "EXTRACTION_SYSTEM_PROMPT",
    "LEXICAL_DUPLICATE_JACCARD",
    "MAX_APPLY_ADJUDICATED_PAIRS",
    "MAX_BACKFILL_LIMIT",
    "MAX_DEDUP_ADJUDICATED_PAIRS",
    "MEMORY_RETRIEVED_EVENT",
    "SEMANTIC_DUPLICATE_COSINE",
    "ActorFacts",
    "AdjudicationPair",
    "AdjudicationParseError",
    "CandidateParseError",
    "ExistingRecord",
    "ExtractionResult",
    "MemoryAdjudicator",
    "MemoryCandidate",
    "MemoryContext",
    "MemoryContextItem",
    "MemoryDecision",
    "MemoryEmbedder",
    "MemoryProvenance",
    "PairAdjudicator",
    "SimilarityVerdict",
    "SourceFacts",
    "SourceRef",
    "agent_team_ids",
    "apply_candidates",
    "authorization_filter",
    "build_adjudication_request",
    "build_extraction_request",
    "build_memory_context",
    "compare_contents",
    "contains_secret",
    "content_hash",
    "create_version",
    "derive_source_facts",
    "evaluate_candidate",
    "extract_candidates",
    "forget_record",
    "is_low_information",
    "is_self_referential",
    "normalize_content",
    "parse_adjudication",
    "parse_candidates",
    "record_retrieval_provenance",
    "resolve_memory_adjudicator",
    "resolve_memory_embedder",
    "screen_content",
    "select_embedding_profile",
    "set_embedding",
    "token_set",
    "unavailable_context",
]
