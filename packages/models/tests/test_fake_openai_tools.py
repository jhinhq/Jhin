"""Fake-provider tool-call emission: the deterministic marker protocol the
Phase 4 exit tests drive a real tool-call roundtrip with."""

import json

from jhin_models import ModelMessage, ModelRequest, build_model_client
from jhin_models.testing import FakeOpenAIServer
from jhin_models.testing.fake_openai import build_completion

MARKER = '[[tool:system.echo {"text": "hello tools"}]]'


def test_marker_produces_tool_call() -> None:
    body = {
        "model": "fake-mini",
        "messages": [{"role": "user", "content": f"Please run this. {MARKER}"}],
    }
    status, payload = build_completion(body)
    assert status == 200
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "system.echo"
    assert json.loads(call["function"]["arguments"]) == {"text": "hello tools"}


def test_tool_result_completes_the_loop() -> None:
    body = {
        "model": "fake-mini",
        "messages": [
            {"role": "user", "content": f"Please run this. {MARKER}"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {"name": "system.echo", "arguments": '{"text": "x"}'},
                    }
                ],
            },
            {"role": "tool", "content": '{"text": "hello tools"}', "tool_call_id": "call_0"},
        ],
    }
    status, payload = build_completion(body)
    assert status == 200
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "Done after 1 tool call(s)" in choice["message"]["content"]
    assert "hello tools" in choice["message"]["content"]


def test_two_markers_emit_sequential_calls() -> None:
    content = '[[tool:system.time {}]] then [[tool:system.echo {"text": "2nd"}]]'
    messages = [{"role": "user", "content": content}]
    status, payload = build_completion({"model": "fake-mini", "messages": messages})
    first = payload["choices"][0]["message"]["tool_calls"][0]
    assert first["function"]["name"] == "system.time"

    messages += [
        {"role": "assistant", "content": None, "tool_calls": [first]},
        {"role": "tool", "content": "{}", "tool_call_id": first["id"]},
    ]
    status, payload = build_completion({"model": "fake-mini", "messages": messages})
    assert status == 200
    second = payload["choices"][0]["message"]["tool_calls"][0]
    assert second["function"]["name"] == "system.echo"


def test_marker_ignored_without_tools_is_not_a_thing() -> None:
    """The fake emits tool calls even when no tools were advertised —
    deliberately simulating a hallucinating model so gateway-side
    authorization (not the prompt) is what the tests exercise (plan 52)."""
    body = {"model": "fake-mini", "messages": [{"role": "user", "content": MARKER}]}
    _status, payload = build_completion(body)
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


async def test_adapter_roundtrip_with_tool_call() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            first = await client.generate(
                ModelRequest(
                    model="fake-mini",
                    messages=(ModelMessage(role="user", content=f"Run it: {MARKER}"),),
                )
            )
            assert first.finish_reason == "tool_calls"
            assert first.tool_calls[0].name == "system.echo"

            second = await client.generate(
                ModelRequest(
                    model="fake-mini",
                    messages=(
                        ModelMessage(role="user", content=f"Run it: {MARKER}"),
                        ModelMessage(role="assistant", content="", tool_calls=first.tool_calls),
                        ModelMessage(
                            role="tool",
                            content='{"text": "hello tools"}',
                            tool_call_id=first.tool_calls[0].id,
                        ),
                    ),
                )
            )
        finally:
            await client.close()

    assert second.finish_reason == "stop"
    assert second.tool_calls == ()
    assert "hello tools" in second.text
