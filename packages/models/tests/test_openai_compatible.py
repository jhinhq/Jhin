"""Adapter tests against an httpx MockTransport (plan 32.1, 32.2)."""

import json
from typing import Any

import httpx
import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest
from jhin_models.providers.openai import OpenAIClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient


def chat_response(model: str = "fake-mini") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from the fake model."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 42,
            "completion_tokens": 7,
            "total_tokens": 49,
            "prompt_tokens_details": {"cached_tokens": 12},
        },
    }


def request_for(model: str = "fake-mini") -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(role="system", content="You are terse."),
            ModelMessage(role="user", content="Say hello."),
        ),
        temperature=0.2,
        max_output_tokens=128,
    )


async def test_generate_parses_text_usage_and_request_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=chat_response())

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    response = await client.generate(request_for())
    await client.close()

    assert seen["url"] == "http://fake/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["max_tokens"] == 128
    assert seen["body"]["temperature"] == 0.2
    assert response.text == "Hello from the fake model."
    assert response.usage.input_tokens == 42
    assert response.usage.output_tokens == 7
    assert response.usage.cached_tokens == 12
    assert response.provider_request_id == "chatcmpl-test-1"
    assert response.finish_reason == "stop"


async def test_openai_adapter_uses_max_completion_tokens() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response("gpt-test"))

    client = OpenAIClient(api_key="sk-test", transport=httpx.MockTransport(handler))
    await client.generate(request_for("gpt-test"))
    await client.close()

    assert seen["body"]["max_completion_tokens"] == 128
    assert "max_tokens" not in seen["body"]


async def test_http_429_is_retryable_and_401_is_not() -> None:
    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler_429)
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for())
    await client.close()
    assert excinfo.value.retryable is True

    def handler_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler_401)
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for())
    await client.close()
    assert excinfo.value.retryable is False
    assert excinfo.value.status_code == 401


async def test_network_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.generate(request_for())
    await client.close()
    assert excinfo.value.retryable is True


async def test_stream_yields_deltas() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    chunks = [chunk async for chunk in client.stream(request_for())]
    await client.close()
    assert "".join(chunks) == "Hello"


async def test_verify_lists_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "fake-mini"}, {"id": "fake-pro"}]})

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    assert await client.verify() == "ok: 2 models visible"
    await client.close()


async def test_list_models_returns_sorted_unique_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-b"}, {"id": "gpt-a"}, {"id": "gpt-b"}, {"object": "x"}]},
        )

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    assert await client.list_models() == ["gpt-a", "gpt-b"]
    await client.close()


async def test_list_models_http_error_is_a_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ModelProviderError) as excinfo:
        await client.list_models()
    await client.close()
    assert excinfo.value.status_code == 401


def test_describe_error_body_extracts_provider_message() -> None:
    from jhin_models.base import describe_error_body

    body = '{"error": {"message": "You exceeded your current quota", "type": "x"}}'
    assert describe_error_body(body) == "You exceeded your current quota"
    assert describe_error_body("plain text") == "plain text"
    assert describe_error_body('{"message": "nested"}') == "nested"
    assert len(describe_error_body("x" * 900)) == 500
