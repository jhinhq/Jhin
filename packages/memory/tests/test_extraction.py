"""Structured candidate extraction: strict parser and the provider call."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from jhin_domain import MemoryKind, MemoryScope
from jhin_memory import (
    EXTRACTION_SYSTEM_PROMPT,
    CandidateParseError,
    build_extraction_request,
    extract_candidates,
    parse_candidates,
)
from jhin_models import ModelClient, ModelProviderError, ModelRequest, ModelResponse, ModelUsage


class StubClient(ModelClient):
    def __init__(self, text: str | None = None, *, error: Exception | None = None) -> None:
        self.text = text or ""
        self.error = error
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(text=self.text, usage=ModelUsage(input_tokens=10, output_tokens=5))

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def close(self) -> None:
        pass


VALID = {
    "candidates": [
        {
            "content": "Varand prefers concise updates.",
            "kind": "preference",
            "subject": "user.update_style",
            "tags": ["Style", "style", " "],
            "confidence": 0.9,
            "importance": 0.6,
            "requested_scope": "agent",
        },
        {"content": "The team deploys on Tuesdays.", "requested_scope": "team"},
    ]
}


class TestParser:
    def test_valid_document(self) -> None:
        parsed = parse_candidates(json.dumps(VALID))
        assert len(parsed) == 2
        assert parsed[0].kind is MemoryKind.PREFERENCE
        assert parsed[0].tags == ("style",)
        assert parsed[1].requested_scope is MemoryScope.TEAM
        assert parsed[1].kind is MemoryKind.OTHER

    def test_fenced_json_is_accepted(self) -> None:
        parsed = parse_candidates("```json\n" + json.dumps(VALID) + "\n```")
        assert len(parsed) == 2

    def test_empty_candidates(self) -> None:
        assert parse_candidates('{"candidates": []}') == []

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Sure! Here is what I remember: the user likes tea.",
            "[fake-mini] Completed: extract memory",
            '{"candidates": [], "notes": "extra"}',
            '{"candidates": {"content": "x"}}',
            '{"candidates": [{"content": ""}]}',
            '{"candidates": [{"content": "x", "status": "active"}]}',
            '{"candidates": [{"content": "x", "requested_scope": "everyone"}]}',
            '{"candidates": [{"content": "x", "confidence": 7}]}',
            '{"candidates": ["just a string"]}',
            '{"candidates": [{"content": "' + "x" * 3000 + '"}]}',
            '{"candidates": [' + ",".join(['{"content": "x"}'] * 21) + "]}",
        ],
    )
    def test_malformed_output_is_rejected(self, text: str) -> None:
        with pytest.raises(CandidateParseError):
            parse_candidates(text)

    def test_model_cannot_set_activation_fields(self) -> None:
        with pytest.raises(CandidateParseError):
            parse_candidates('{"candidates": [{"content": "x", "explicit": true}]}')


class TestExtraction:
    def test_request_shape(self) -> None:
        request = build_extraction_request(
            model="fake-mini", source_text="hello " * 10_000, agent_name="Ava"
        )
        assert request.model == "fake-mini"
        assert request.temperature == 0.0
        assert request.messages[0].role == "system"
        assert "Ava" in request.messages[1].content
        assert len(request.messages[1].content) < 13_000
        assert "<known_memories>" not in request.messages[1].content

    def test_prompt_excludes_self_facts_and_demands_consolidation(self) -> None:
        prompt = EXTRACTION_SYSTEM_PROMPT
        assert "Never propose facts about the AI teammate itself" in prompt
        assert "greetings" in prompt
        assert "ONE consolidated fact" in prompt
        assert "only NEW or CHANGED facts" in prompt

    def test_existing_memories_are_listed_and_bounded(self) -> None:
        request = build_extraction_request(
            model="fake-mini",
            source_text="user: hi",
            agent_name="Ava",
            existing_memories=["We deploy every other Thursday.", "x" * 500] + [""] * 3,
        )
        content = request.messages[1].content
        assert "<known_memories>" in content
        assert "- We deploy every other Thursday." in content
        # Entries are truncated and blanks skipped.
        assert "x" * 201 not in content
        assert "propose only NEW or CHANGED facts" in content.replace("\n", " ")

    async def test_success(self) -> None:
        client = StubClient(json.dumps(VALID))
        result = await extract_candidates(
            client, model="fake-mini", source_text="t", agent_name="Ava"
        )
        assert result.ok
        assert len(result.candidates) == 2
        assert result.input_tokens == 10

    async def test_malformed_output_is_a_typed_failure(self) -> None:
        client = StubClient("[fake-mini] Completed: t")
        result = await extract_candidates(
            client, model="fake-mini", source_text="t", agent_name="Ava"
        )
        assert not result.ok
        assert result.error.startswith("malformed_output")
        assert result.candidates == []

    async def test_provider_error_never_raises(self) -> None:
        client = StubClient(error=ModelProviderError("boom", status_code=500, retryable=True))
        result = await extract_candidates(
            client, model="fake-mini", source_text="t", agent_name="Ava"
        )
        assert not result.ok
        assert "provider_error" in result.error

    async def test_unexpected_error_never_raises(self) -> None:
        client = StubClient(error=RuntimeError("kaboom"))
        result = await extract_candidates(
            client, model="fake-mini", source_text="t", agent_name="Ava"
        )
        assert not result.ok
        assert result.error == "RuntimeError"
