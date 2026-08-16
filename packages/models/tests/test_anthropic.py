"""Anthropic adapter tests against an httpx MockTransport."""

import json
from typing import Any

import httpx

from jhin_models import ModelMessage, ModelRequest
from jhin_models.providers.anthropic import AnthropicClient


def messages_response() -> dict[str, Any]:
    return {
        "id": "msg_test_1",
        "type": "message",
        "model": "claude-test",
        "content": [{"type": "text", "text": "Hello from Claude."}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 30,
            "output_tokens": 9,
            "cache_read_input_tokens": 5,
        },
    }


async def test_generate_extracts_system_and_parses_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=messages_response())

    client = AnthropicClient(api_key="sk-ant-test", transport=httpx.MockTransport(handler))
    response = await client.generate(
        ModelRequest(
            model="claude-test",
            messages=(
                ModelMessage(role="system", content="You are terse."),
                ModelMessage(role="user", content="Say hello."),
            ),
            max_output_tokens=256,
        )
    )
    await client.close()

    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    # System prompt moves to the top-level field, not the messages array.
    assert seen["body"]["system"] == "You are terse."
    assert all(m["role"] != "system" for m in seen["body"]["messages"])
    assert seen["body"]["max_tokens"] == 256

    assert response.text == "Hello from Claude."
    assert response.usage.input_tokens == 30
    assert response.usage.output_tokens == 9
    assert response.usage.cached_tokens == 5
    assert response.finish_reason == "end_turn"
    assert response.provider_request_id == "msg_test_1"


async def test_max_tokens_defaults_when_unset() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=messages_response())

    client = AnthropicClient(api_key="sk-ant-test", transport=httpx.MockTransport(handler))
    await client.generate(
        ModelRequest(model="claude-test", messages=(ModelMessage(role="user", content="hi"),))
    )
    await client.close()
    assert seen["body"]["max_tokens"] == 4096


async def test_stream_yields_text_deltas() -> None:
    sse = (
        'data: {"type":"message_start","message":{}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )

    client = AnthropicClient(api_key="sk-ant-test", transport=httpx.MockTransport(handler))
    chunks = [
        chunk
        async for chunk in client.stream(
            ModelRequest(model="claude-test", messages=(ModelMessage(role="user", content="hi"),))
        )
    ]
    await client.close()
    assert "".join(chunks) == "Hello"
