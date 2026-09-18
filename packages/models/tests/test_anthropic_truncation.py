"""Anthropic's word for a truncated answer reaches the runtime as Jhin's.

The Messages API calls the output limit ``max_tokens``; the worker refuses a
truncated completion by the OpenAI spelling ``length``. Both the buffered and
the streaming path must report the same thing, or a run cut off mid-sentence
is committed as a finished answer.
"""

from __future__ import annotations

from typing import Any

import httpx

from jhin_models import ModelMessage, ModelRequest
from jhin_models.providers.anthropic import AnthropicClient


def truncated_response() -> dict[str, Any]:
    return {
        "id": "msg_truncated",
        "type": "message",
        "model": "claude-test",
        "content": [{"type": "text", "text": "Good - Varand answered. Let"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 30, "output_tokens": 9},
    }


def client(handler: Any) -> AnthropicClient:
    return AnthropicClient(api_key="sk-ant-test", transport=httpx.MockTransport(handler))


async def test_generate_reports_max_tokens_as_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=truncated_response())

    adapter = client(handler)
    response = await adapter.generate(
        ModelRequest(model="claude-test", messages=(ModelMessage(role="user", content="hi"),))
    )
    await adapter.close()

    assert response.finish_reason == "length"


async def test_stream_reports_max_tokens_as_length() -> None:
    sse = (
        'data: {"type":"message_start","message":{"id":"msg_truncated","model":"claude-test"}}\n\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Let"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        '"usage":{"output_tokens":9}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )

    adapter = client(handler)
    completed = [
        event
        async for event in adapter.stream_events(
            ModelRequest(model="claude-test", messages=(ModelMessage(role="user", content="hi"),))
        )
        if event.type == "completed"
    ]
    await adapter.close()

    assert completed[0].response is not None
    assert completed[0].response.finish_reason == "length"


async def test_other_stop_reasons_are_passed_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = truncated_response() | {"stop_reason": "end_turn"}
        return httpx.Response(200, json=body)

    adapter = client(handler)
    response = await adapter.generate(
        ModelRequest(model="claude-test", messages=(ModelMessage(role="user", content="hi"),))
    )
    await adapter.close()

    assert response.finish_reason == "end_turn"
