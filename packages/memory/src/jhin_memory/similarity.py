"""Deterministic content-similarity primitives for memory dedup.

Pure functions shared by the write-path policy (near-duplicate detection in
:mod:`jhin_memory.policy`), retrieval ranking (:mod:`jhin_memory.retrieval`),
and the retroactive dedup service (``POST /memories/deduplicate``).

Two signals, combined conservatively:

- **embedding cosine** over stored vectors — only when both sides carry an
  embedding from the same model (mixed models/dimensions are never compared);
- **lexical token-set overlap** (stopword-stripped Jaccard / containment) —
  always available, and the only signal when embeddings are absent.

A pair is a *near duplicate* when any of these holds:

- cosine ≥ :data:`SEMANTIC_DUPLICATE_COSINE` (0.90),
- Jaccard ≥ :data:`LEXICAL_DUPLICATE_JACCARD` (0.6),
- same normalized ``subject`` **and** Jaccard ≥ 0.4 **and** token containment
  ≥ 0.75 (wording variants of the same keyed fact; a genuinely different
  *value* for the subject keeps failing this and stays on the contradiction
  path).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

SEMANTIC_DUPLICATE_COSINE = 0.90
LEXICAL_DUPLICATE_JACCARD = 0.6
SUBJECT_NEAR_JACCARD = 0.4
SUBJECT_NEAR_CONTAINMENT = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "we",
        "they",
        "he",
        "she",
        "my",
        "our",
        "your",
        "their",
        "me",
        "us",
        "them",
        "do",
        "does",
        "did",
        "not",
        "no",
        "yes",
        "at",
        "by",
        "from",
        "as",
    ]
)


def tokenize(text: str) -> list[str]:
    """Lowercased informative tokens (stopwords removed), in order."""
    return [tok for tok in _TOKEN_RE.findall(text.casefold()) if tok not in _STOPWORDS]


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokenize(text))


def cosine(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Cosine similarity, or ``None`` when the vectors are not comparable."""
    if len(a) != len(b) or not a:
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return max(-1.0, min(1.0, dot / (na * nb)))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap relative to the smaller set (wording-variant detector)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(frozen=True)
class SimilarityVerdict:
    near_duplicate: bool
    jaccard: float
    cosine: float | None
    subject_match: bool

    @property
    def score(self) -> float:
        return max(self.jaccard, self.cosine if self.cosine is not None else 0.0)


def compare_contents(
    content_a: str,
    content_b: str,
    *,
    subject_a: str | None = None,
    subject_b: str | None = None,
    embedding_a: Sequence[float] | None = None,
    embedding_b: Sequence[float] | None = None,
    embedding_model_a: str | None = None,
    embedding_model_b: str | None = None,
) -> SimilarityVerdict:
    """The one shared near-duplicate rule (see module docstring)."""
    tokens_a = token_set(content_a)
    tokens_b = token_set(content_b)
    jac = jaccard(tokens_a, tokens_b)
    cos: float | None = None
    if (
        embedding_a
        and embedding_b
        and embedding_model_a is not None
        and embedding_model_a == embedding_model_b
    ):
        cos = cosine(embedding_a, embedding_b)
    subject_match = subject_a is not None and subject_a == subject_b
    near = (
        (cos is not None and cos >= SEMANTIC_DUPLICATE_COSINE)
        or jac >= LEXICAL_DUPLICATE_JACCARD
        or (
            subject_match
            and jac >= SUBJECT_NEAR_JACCARD
            and containment(tokens_a, tokens_b) >= SUBJECT_NEAR_CONTAINMENT
        )
    )
    return SimilarityVerdict(
        near_duplicate=near, jaccard=jac, cosine=cos, subject_match=subject_match
    )
