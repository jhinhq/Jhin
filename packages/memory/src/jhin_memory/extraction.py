"""Structured memory-candidate extraction through the model provider layer.

The model is asked for a strict JSON document; :func:`parse_candidates`
rejects anything that is not exactly ``{"candidates": [...]}`` with
schema-valid entries. The model can propose content, kind, subject, tags,
confidence, importance, and a *requested* scope — it can never activate a
memory, choose a source, or broaden visibility (that is policy code).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from jhin_memory.types import MAX_CANDIDATES_PER_EXTRACTION, MemoryCandidate
from jhin_models import ModelClient, ModelMessage, ModelProviderError, ModelRequest

MAX_SOURCE_CHARS = 12_000
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

EXTRACTION_SYSTEM_PROMPT = (
    "You extract durable, reusable memory from a transcript for an AI teammate. "
    "Return ONLY a JSON object of the form "
    '{"candidates": [{"content": str, "kind": "fact|preference|decision|procedure|context|other", '
    '"subject": str|null, "tags": [str], "confidence": 0..1, "importance": 0..1, '
    '"requested_scope": "agent|team|workspace"}]} with no prose and no markdown. '
    "Rules: each candidate is one concise, self-contained sentence (max 300 characters); "
    "never include secrets, credentials, tokens, passwords, connection strings, or "
    "authorization headers; never copy the transcript verbatim; skip transient chit-chat, "
    "greetings, and small talk. "
    "Never propose facts about the AI teammate itself — its name, its role, that it is an "
    "AI/assistant/teammate, or anything already stated in its own system prompt or identity — "
    "and never restate how this platform or its tooling works. "
    "Facts must be about the user, the people or team the teammate works with, the company, "
    "external systems, decisions, or preferences. "
    "Prefer ONE consolidated fact over several wording variants of the same fact. "
    "When a list of already-remembered facts is provided, propose only NEW or CHANGED facts — "
    "never re-propose an existing fact in different words; "
    'use "subject" as a short stable key (e.g. "deploy.day") when the memory states a value '
    "for something that could later change; prefer requested_scope=agent unless the "
    "information is clearly about the whole team or company. "
    'Return {"candidates": []} when nothing is worth remembering.'
)

# Bounds for the "already remembered" context block sent with each request.
MAX_EXISTING_MEMORIES = 40
MAX_EXISTING_MEMORY_CHARS = 200


class CandidateParseError(ValueError):
    """Model output was not a strictly valid candidate document."""


@dataclass
class ExtractionResult:
    candidates: list[MemoryCandidate] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error[:200],
            "candidate_count": len(self.candidates),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def parse_candidates(text: str) -> list[MemoryCandidate]:
    """Deterministic strict parser. Raises :class:`CandidateParseError`."""
    raw = text.strip()
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1)
    if not raw:
        raise CandidateParseError("empty output")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CandidateParseError(f"not JSON: {exc.msg}") from None
    if not isinstance(document, dict) or set(document.keys()) != {"candidates"}:
        raise CandidateParseError("top level must be an object with exactly 'candidates'")
    entries = document["candidates"]
    if not isinstance(entries, list):
        raise CandidateParseError("'candidates' must be a list")
    if len(entries) > MAX_CANDIDATES_PER_EXTRACTION:
        raise CandidateParseError(f"too many candidates (> {MAX_CANDIDATES_PER_EXTRACTION})")
    parsed: list[MemoryCandidate] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise CandidateParseError(f"candidate {index} is not an object")
        normalized = dict(entry)
        if normalized.get("subject") is None:
            normalized.pop("subject", None)
        if normalized.get("tags") is None:
            normalized.pop("tags", None)
        try:
            parsed.append(MemoryCandidate.model_validate(normalized))
        except ValidationError as exc:
            errors = exc.errors()
            detail = f"{errors[0]['loc']} {errors[0]['msg']}" if errors else "schema"
            raise CandidateParseError(f"candidate {index} invalid: {detail}") from None
    return parsed


def build_extraction_request(
    *,
    model: str,
    source_text: str,
    agent_name: str,
    existing_memories: Sequence[str] = (),
    max_output_tokens: int = 1_500,
) -> ModelRequest:
    bounded = source_text[:MAX_SOURCE_CHARS]
    known = "\n".join(
        f"- {memory[:MAX_EXISTING_MEMORY_CHARS]}"
        for memory in list(existing_memories)[:MAX_EXISTING_MEMORIES]
        if memory.strip()
    )
    known_block = (
        (
            "The teammate already remembers these facts — propose only NEW or CHANGED "
            f"facts, never a rewording of one of these:\n<known_memories>\n{known}\n"
            "</known_memories>\n\n"
        )
        if known
        else ""
    )
    user = (
        f"The AI teammate is named {agent_name}. {known_block}Extract memory candidates "
        f"from the following transcript.\n\n<transcript>\n{bounded}\n</transcript>"
    )
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            ModelMessage(role="user", content=user),
        ),
        temperature=0.0,
        max_output_tokens=max_output_tokens,
    )


async def extract_candidates(
    client: ModelClient,
    *,
    model: str,
    source_text: str,
    agent_name: str,
    existing_memories: Sequence[str] = (),
) -> ExtractionResult:
    """Ask the model once; never raises — failures are returned as a typed
    result so maintenance can record them without failing the origin."""
    request = build_extraction_request(
        model=model,
        source_text=source_text,
        agent_name=agent_name,
        existing_memories=existing_memories,
    )
    try:
        response = await client.generate(request)
    except ModelProviderError as exc:
        return ExtractionResult(ok=False, error=f"provider_error: {exc}")
    except Exception as exc:
        return ExtractionResult(ok=False, error=f"{type(exc).__name__}")
    usage = response.usage
    try:
        candidates = parse_candidates(response.text)
    except CandidateParseError as exc:
        return ExtractionResult(
            ok=False,
            error=f"malformed_output: {exc}",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    return ExtractionResult(
        candidates=candidates,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
