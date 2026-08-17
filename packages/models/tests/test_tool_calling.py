"""OpenAI-style function calling through the adapters (plan 12, 7.3)."""

import json
from typing import Any

import httpx

from jhin_models import ModelMessage, ModelRequest, ModelToolCall, ToolSchema
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient

ECHO_TOOL = ToolSchema(
    name="system.echo",
    description="Echo the input back.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


def _tool_call_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-tools-1",
        "model": "fake-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {
                                "name": "system.echo",
                                "arguments": '{"text": "hi"}',
                            },
                        },
                        {"id": "", "type": "function", "function": {"name": "bad"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def test_openai_compatible_sends_tools_and_parses_tool_calls() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_tool_call_response())

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    response = await client.generate(
        ModelRequest(
            model="fake-mini",
            messages=(ModelMessage(role="user", content="Echo hi."),),
            tools=(ECHO_TOOL,),
        )
    )
    await client.close()

    sent_tool = seen["body"]["tools"][0]
    assert sent_tool["type"] == "function"
    assert sent_tool["function"]["name"] == "system.echo"
    assert sent_tool["function"]["parameters"]["properties"]["text"]["type"] == "string"

    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1  # the malformed entry is dropped
    call = response.tool_calls[0]
    assert call.id == "call_0"
    assert call.name == "system.echo"
    assert json.loads(call.arguments_json) == {"text": "hi"}


async def test_openai_compatible_serializes_tool_exchange_messages() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "fake-mini",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "done"}}
                ],
                "usage": {},
            },
        )

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    await client.generate(
        ModelRequest(
            model="fake-mini",
            messages=(
                ModelMessage(role="user", content="Echo hi."),
                ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ModelToolCall(
                            id="call_0", name="system.echo", arguments_json='{"text": "hi"}'
                        ),
                    ),
                ),
                ModelMessage(role="tool", content='{"text": "hi"}', tool_call_id="call_0"),
            ),
        )
    )
    await client.close()

    assistant_wire = seen["body"]["messages"][1]
    assert assistant_wire["tool_calls"][0]["function"]["name"] == "system.echo"
    tool_wire = seen["body"]["messages"][2]
    assert tool_wire["role"] == "tool"
    assert tool_wire["tool_call_id"] == "call_0"


async def test_anthropic_maps_tools_and_tool_use_blocks() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "Calling the tool."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "system.echo",
                        "input": {"text": "hi"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    client = AnthropicClient(api_key="sk-ant-test", transport=httpx.MockTransport(handler))
    response = await client.generate(
        ModelRequest(
            model="claude-test",
            messages=(
                ModelMessage(role="user", content="Echo hi."),
                ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ModelToolCall(
                            id="toolu_0", name="system.echo", arguments_json='{"text": "prev"}'
                        ),
                    ),
                ),
                ModelMessage(role="tool", content='{"text": "prev"}', tool_call_id="toolu_0"),
            ),
            tools=(ECHO_TOOL,),
        )
    )
    await client.close()

    assert seen["body"]["tools"][0]["input_schema"]["properties"]["text"]["type"] == "string"
    # Prior assistant tool call became a tool_use block…
    assistant_blocks = seen["body"]["messages"][1]["content"]
    assert any(b["type"] == "tool_use" and b["id"] == "toolu_0" for b in assistant_blocks)
    # …and the tool result became a user-role tool_result block.
    result_wire = seen["body"]["messages"][2]
    assert result_wire["role"] == "user"
    assert result_wire["content"][0]["type"] == "tool_result"
    assert result_wire["content"][0]["tool_use_id"] == "toolu_0"

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "system.echo"
    assert json.loads(response.tool_calls[0].arguments_json) == {"text": "hi"}
