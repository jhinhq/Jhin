import json

import httpx
import pytest

from jhin_models import ModelMessage, ModelRequest
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_openai_stream_preserves_split_tool_arguments_usage_and_completion():
    chunks = [
        {
            "id": "req1",
            "model": "model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "Checking ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "function": {"name": "cli__file__read", "arguments": '{"pa'},
                            }
                        ],
                    },
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "files",
                        "tool_calls": [{"index": 0, "function": {"arguments": 'th":"x"}'}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8}},
    ]
    client = OpenAICompatibleClient(
        base_url="https://local.test/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                + "data: [DONE]\n\n",
            )
        ),
    )
    events = [
        event
        async for event in client.stream_events(
            ModelRequest(model="model", messages=(ModelMessage(role="user", content="read"),))
        )
    ]
    assert [e.text for e in events if e.type == "text_delta"] == ["Checking ", "files"]
    final = events[-1].response
    assert final.text == "Checking files"
    assert final.tool_calls[0].arguments_json == '{"path":"x"}'
    assert final.tool_calls[0].name == "cli.file.read"
    assert final.usage.input_tokens == 12 and final.usage.output_tokens == 8
    await client.close()


@pytest.mark.asyncio
async def test_anthropic_stream_joins_partial_json_and_usage_without_reasoning():
    chunks = [
        {
            "type": "message_start",
            "message": {"id": "req", "model": "claude", "usage": {"input_tokens": 10}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "tool", "name": "read", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "private"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    client = AnthropicClient(
        api_key="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
            )
        ),
    )
    events = [
        e
        async for e in client.stream_events(
            ModelRequest(model="claude", messages=(ModelMessage(role="user", content="read"),))
        )
    ]
    assert events[-1].response.tool_calls[0].arguments_json == '{"x":1}'
    assert events[-1].response.usage.input_tokens == 10
    assert "private" not in str(events)
    await client.close()


def test_images_translate_as_typed_blocks_on_both_provider_families():
    message = ModelMessage(
        role="user",
        content="Describe",
        content_parts=({"type": "image", "mime_type": "image/png", "data_base64": "aGVsbG8="},),
    )
    oa = OpenAICompatibleClient(base_url="https://local.test")
    an = AnthropicClient(api_key="test")
    assert (
        oa._serialize_message(message)["content"][1]["image_url"]["url"]
        == "data:image/png;base64,aGVsbG8="
    )
    assert an._serialize_message(message)["content"][1]["source"]["data"] == "aGVsbG8="
