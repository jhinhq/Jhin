"""The fake provider must behave like a real OpenAI-compatible endpoint
through our own adapter — otherwise it proves nothing (plan 32.2)."""

import json

import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest, build_model_client
from jhin_models.testing import FakeOpenAIServer
from jhin_models.testing.fake_openai import FAIL_MODEL, build_completion


async def test_adapter_roundtrip_against_fake_server() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            response = await client.generate(
                ModelRequest(
                    model="fake-mini",
                    messages=(ModelMessage(role="user", content="Say hello to Jhin"),),
                )
            )
        finally:
            await client.close()

    assert response.text.startswith("[fake-mini] Completed:")
    assert "Say hello to Jhin" in response.text
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.finish_reason == "stop"


async def test_verify_lists_models() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            detail = await client.verify()
        finally:
            await client.close()
    assert "2 models" in detail


async def test_fail_model_surfaces_provider_error() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            with pytest.raises(ModelProviderError):
                await client.generate(
                    ModelRequest(
                        model=FAIL_MODEL,
                        messages=(ModelMessage(role="user", content="boom"),),
                    )
                )
        finally:
            await client.close()


def test_completion_is_deterministic() -> None:
    body = {"model": "fake-pro", "messages": [{"role": "user", "content": "same input"}]}
    assert build_completion(body) == build_completion(body)


# --- deterministic memory extraction (jhin_memory.extraction contract) ---

_EXTRACTION_SYSTEM = (
    "You extract durable, reusable memory from a transcript for an AI teammate. "
    'Return ONLY a JSON object of the form {"candidates": [...]}.'
)


def _extraction_body(user: str) -> dict[str, object]:
    return {
        "model": "fake-mini",
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {"role": "user", "content": user},
        ],
    }


def test_memory_extraction_returns_strict_candidates() -> None:
    user = (
        "The AI teammate is named Ava. Extract memory candidates from the following "
        "transcript.\n\n<transcript>\nuser: Please remember that we deploy every other "
        "Thursday.\nassistant: Noted!\n</transcript>"
    )
    status, envelope = build_completion(_extraction_body(user))
    assert status == 200
    reply = envelope["choices"][0]["message"]["content"]  # type: ignore[index]
    document = json.loads(reply)
    assert list(document.keys()) == ["candidates"]
    assert document["candidates"][0]["content"] == "we deploy every other Thursday"
    assert document["candidates"][0]["kind"] == "fact"
    assert build_completion(_extraction_body(user)) == build_completion(_extraction_body(user))


def test_memory_extraction_skips_known_memories() -> None:
    user = (
        "The AI teammate is named Ava. The teammate already remembers these facts — propose "
        "only NEW or CHANGED facts, never a rewording of one of these:\n<known_memories>\n"
        "- We deploy every other Thursday.\n</known_memories>\n\nExtract memory candidates "
        "from the following transcript.\n\n<transcript>\nuser: Remember: the release day is "
        "every other Thursday.\n</transcript>"
    )
    status, envelope = build_completion(_extraction_body(user))
    assert status == 200
    reply = envelope["choices"][0]["message"]["content"]  # type: ignore[index]
    assert json.loads(reply) == {"candidates": []}


def test_memory_extraction_learns_from_a_delegation_exchange() -> None:
    user = (
        "The AI teammate is named SWE. Extract memory candidates from the following "
        "transcript.\n\n<transcript>\ntask: Write the deploy docs\n"
        "delegation from CTO (delegation)\ndescription: Document the deploy pipeline.\n"
        "</transcript>"
    )
    status, envelope = build_completion(_extraction_body(user))
    assert status == 200
    reply = envelope["choices"][0]["message"]["content"]  # type: ignore[index]
    document = json.loads(reply)
    assert document["candidates"][0]["content"] == "CTO delegated: Write the deploy docs"


def test_ordinary_chat_is_not_intercepted() -> None:
    status, envelope = build_completion(
        {"model": "fake-mini", "messages": [{"role": "user", "content": "remember me?"}]}
    )
    assert status == 200
    reply = envelope["choices"][0]["message"]["content"]  # type: ignore[index]
    assert reply.startswith("[fake-mini] Completed:")
