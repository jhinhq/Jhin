"""Ollama's native ``/api/chat`` wire, in both directions.

Every fixture here is a recorded shape from the reference host (Ollama 0.34.1,
qwen3.8): tool calls carry an id and an arguments *object*, a stream is NDJSON
whose tool calls arrive whole rather than as deltas, and the final line carries
``done_reason`` with the token counts. Nothing here talks to a real Ollama.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jhin_models import (
    ModelContent,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelToolCall,
    ToolSchema,
)
from jhin_models.providers.ollama import _LOAD_TIMEOUT, OllamaClient

SEARCH = ToolSchema(
    name="ghost.archive.search",
    description="Search the archive",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)
# The call the reference host returned, verbatim: an id, and arguments as an
# object rather than the JSON string OpenAI-compatible providers return.
NATIVE_CALL: dict[str, Any] = {
    "id": "call_3rq2qq9d",
    "function": {
        "index": 0,
        "name": "ghost__archive__search",
        "arguments": {"query": "creator burnout"},
    },
}


def done(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "qwen3.8:latest",
        "created_at": "2026-09-02T10:00:00Z",
        "message": {"role": "assistant", "content": "Done."},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1_000_000,
        "load_duration": 1_000,
        "prompt_eval_count": 6_755,
        "prompt_eval_cached_count": 4_096,
        "prompt_eval_duration": 900_000,
        "eval_count": 128,
        "eval_duration": 100_000,
    }
    body.update(overrides)
    return body


def ndjson(lines: list[dict[str, Any]]) -> bytes:
    return "".join(f"{json.dumps(line)}\n" for line in lines).encode()


def client(handler: Any) -> OllamaClient:
    return OllamaClient(transport=httpx.MockTransport(handler))


def recording(response: httpx.Response) -> tuple[OllamaClient, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return response

    return client(handler), seen


def request_for(
    *messages: ModelMessage, tools: tuple[ToolSchema, ...] = (SEARCH,), **kwargs: Any
) -> ModelRequest:
    return ModelRequest(
        model="qwen3.8:latest",
        messages=messages or (ModelMessage(role="user", content="hi"),),
        tools=tools,
        **kwargs,
    )


async def test_a_tool_call_survives_the_object_to_string_conversion() -> None:
    adapter, _ = recording(httpx.Response(200, json=done(message={"tool_calls": [NATIVE_CALL]})))

    response = await adapter.generate(request_for())
    await adapter.close()

    call = response.tool_calls[0]
    # The id is what Jhin pairs a result by, so it travels verbatim; the
    # arguments become the JSON text every consumer downstream already reads.
    assert call.id == "call_3rq2qq9d"
    assert call.name == "ghost.archive.search"
    assert json.loads(call.arguments_json) == {"query": "creator burnout"}


async def test_a_streamed_tool_call_arrives_whole_and_keeps_its_id() -> None:
    """No delta accumulator: the whole call lands in one NDJSON line, and two
    calls in separate lines are two calls, not one merged pair."""
    second = {
        "id": "call_ab12cd34",
        "function": {"index": 0, "name": "ghost__archive__search", "arguments": {"query": "burn"}},
    }
    adapter = client(
        lambda _request: httpx.Response(
            200,
            content=ndjson(
                [
                    {
                        "model": "qwen3.8:latest",
                        "message": {"role": "assistant", "content": "Look"},
                    },
                    {"model": "qwen3.8:latest", "message": {"tool_calls": [NATIVE_CALL]}},
                    {"model": "qwen3.8:latest", "message": {"tool_calls": [second]}},
                    done(message={"role": "assistant", "content": ""}),
                ]
            ),
        )
    )

    events = [event async for event in adapter.stream_events(request_for())]
    await adapter.close()

    completed = events[-1].response
    assert completed is not None
    assert [(call.id, call.name) for call in completed.tool_calls] == [
        ("call_3rq2qq9d", "ghost.archive.search"),
        ("call_ab12cd34", "ghost.archive.search"),
    ]
    assert [json.loads(call.arguments_json) for call in completed.tool_calls] == [
        {"query": "creator burnout"},
        {"query": "burn"},
    ]
    assert [event.text for event in events if event.type == "text_delta"] == ["Look"]


async def test_a_call_without_an_id_is_still_dispatchable() -> None:
    """Ids came late to Ollama. Dropping an id-less call would strand the
    agent mid-task; a synthesised one still pairs its result within the turn."""
    adapter, _ = recording(
        httpx.Response(
            200,
            json=done(
                message={
                    "tool_calls": [
                        {"function": {"name": "ghost__archive__search", "arguments": {}}}
                    ]
                }
            ),
        )
    )

    response = await adapter.generate(request_for())
    await adapter.close()

    assert response.tool_calls[0].id
    assert response.tool_calls[0].name == "ghost.archive.search"


async def test_a_result_whose_call_is_out_of_view_still_serialises() -> None:
    """Conversation history can carry a result whose call turn was trimmed
    long ago; that turn must travel, unnamed, not blow up the request."""
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(
        request_for(ModelMessage(role="tool", content="3 posts", tool_call_id="call_gone"))
    )
    await adapter.close()

    assert seen["body"]["messages"][0] == {
        "role": "tool",
        "content": "3 posts",
        "tool_call_id": "call_gone",
    }


async def test_streamed_text_arrives_as_it_comes_and_ends_with_the_usage() -> None:
    adapter = client(
        lambda _request: httpx.Response(
            200,
            content=ndjson(
                [
                    {"model": "qwen3.8:latest", "message": {"content": "Once "}},
                    {"model": "qwen3.8:latest", "message": {"content": "upon"}},
                    done(message={"role": "assistant", "content": ""}),
                ]
            ),
        )
    )

    events = [event async for event in adapter.stream_events(request_for())]
    await adapter.close()

    assert [event.text for event in events if event.type == "text_delta"] == ["Once ", "upon"]
    completed = events[-1].response
    assert completed is not None
    assert completed.text == "Once upon"
    assert completed.finish_reason == "stop"
    assert completed.usage.input_tokens == 6_755
    assert completed.usage.output_tokens == 128


async def test_the_plain_text_stream_rides_the_same_native_route() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            content=ndjson(
                [{"message": {"content": "hi"}}, done(message={"role": "assistant", "content": ""})]
            ),
        )

    adapter = client(handler)

    chunks = [chunk async for chunk in adapter.stream(request_for())]
    await adapter.close()

    assert chunks == ["hi"]
    assert seen["path"] == "/api/chat"


@pytest.mark.parametrize("streamed", [False, True])
async def test_the_output_limit_keeps_the_word_the_worker_refuses_by(streamed: bool) -> None:
    """``model_output_truncated`` is raised on exactly this finish reason, and
    a half-written article committed as a finished one is the bug it stops."""
    body = done(message={"role": "assistant", "content": "The article beg"}, done_reason="length")
    adapter = client(
        lambda _request: httpx.Response(
            200, content=ndjson([body]) if streamed else None, json=None if streamed else body
        )
    )

    if streamed:
        events = [event async for event in adapter.stream_events(request_for())]
        response = events[-1].response
    else:
        response = await adapter.generate(request_for())
    await adapter.close()

    assert response is not None
    assert response.finish_reason == "length"


async def test_usage_maps_from_the_native_counters() -> None:
    adapter, _ = recording(httpx.Response(200, json=done()))

    response = await adapter.generate(request_for())
    await adapter.close()

    assert response.usage.input_tokens == 6_755
    assert response.usage.output_tokens == 128
    # Ollama reports a reused prefix separately; so does Jhin.
    assert response.usage.cached_tokens == 4_096
    assert response.model == "qwen3.8:latest"


async def test_a_tool_result_names_the_tool_it_answers() -> None:
    """Measured against the reference host: a result round trip works when the
    turn carries the tool's name, which Jhin only knows from the call above it."""
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(
        request_for(
            ModelMessage(role="user", content="find it"),
            ModelMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ModelToolCall(
                        id="call_3rq2qq9d",
                        name="ghost.archive.search",
                        arguments_json='{"query": "creator burnout"}',
                    ),
                ),
            ),
            ModelMessage(role="tool", content="3 posts", tool_call_id="call_3rq2qq9d"),
        )
    )
    await adapter.close()

    assistant, result = seen["body"]["messages"][1:]
    assert assistant["tool_calls"] == [
        {
            "id": "call_3rq2qq9d",
            # An object on this wire, where OpenAI types it as a string.
            "function": {
                "name": "ghost__archive__search",
                "arguments": {"query": "creator burnout"},
            },
        }
    ]
    assert result == {
        "role": "tool",
        "content": "3 posts",
        "tool_call_id": "call_3rq2qq9d",
        "tool_name": "ghost__archive__search",
    }


async def test_arguments_that_were_never_an_object_do_not_break_the_echo() -> None:
    """The gateway already refused this call; echoing the turn back must not
    cost the whole step a 400 from a server that types arguments as a map."""
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(
        request_for(
            ModelMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ModelToolCall(id="c1", name="ghost.archive.search", arguments_json="not json"),
                ),
            )
        )
    )
    await adapter.close()

    assert seen["body"]["messages"][0]["tool_calls"][0]["function"]["arguments"] == {}


async def test_an_image_travels_in_the_native_images_array() -> None:
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(
        request_for(
            ModelMessage(
                role="user",
                content="what is this",
                content_parts=(
                    ModelContent(type="image", mime_type="image/png", data_base64="QUJD"),
                    ModelContent(type="text", text="be brief"),
                ),
            )
        )
    )
    await adapter.close()

    message = seen["body"]["messages"][0]
    # Native chat has no content blocks: raw base64 beside the turn's text.
    assert message["images"] == ["QUJD"]
    assert message["content"] == "what is this\nbe brief"


async def test_tools_keep_their_inlined_schemas_on_the_native_route() -> None:
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(
        request_for(
            tools=(
                ToolSchema(
                    name="ghost.assignment.create",
                    description="Create one",
                    parameters={
                        "type": "object",
                        "properties": {"brief": {"$ref": "#/$defs/Brief"}},
                        "$defs": {"Brief": {"type": "object", "properties": {}}},
                    },
                ),
            )
        )
    )
    await adapter.close()

    parameters = seen["body"]["tools"][0]["function"]["parameters"]
    assert seen["body"]["tools"][0]["function"]["name"] == "ghost__assignment__create"
    # Ollama's typed ToolProperty drops $ref before the model template sees it.
    assert "$defs" not in parameters
    assert parameters["properties"]["brief"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("-1", -1), ("0", 0), ("5m", "5m"), (300, 300)],
)
async def test_keep_alive_sentinels_stay_json_numbers(configured: object, expected: object) -> None:
    """Go's duration parser has no spelling for "forever": the ``-1``/``0``
    sentinels are only understood as numbers, and the strings are a 400."""
    adapter, seen = recording(httpx.Response(200, json=done()))

    await adapter.generate(request_for(extra={"keep_alive": configured}))
    await adapter.close()

    assert seen["body"]["keep_alive"] == expected
    assert isinstance(seen["body"]["keep_alive"], type(expected))


async def test_an_unusable_keep_alive_is_refused_before_the_call() -> None:
    adapter, seen = recording(httpx.Response(200, json=done()))

    with pytest.raises(ModelProviderError) as raised:
        await adapter.generate(request_for(extra={"keep_alive": "forever"}))
    await adapter.close()

    assert raised.value.retryable is False
    assert "body" not in seen


async def test_a_refusal_keeps_its_retryable_classification() -> None:
    adapter = client(lambda _request: httpx.Response(503, json={"error": "server busy"}))

    with pytest.raises(ModelProviderError) as raised:
        await adapter.generate(request_for())
    await adapter.close()

    assert raised.value.status_code == 503
    assert raised.value.retryable is True
    # Ollama's own ``{"error": "..."}`` sentence is the one worth showing.
    assert "server busy" in str(raised.value)


async def test_a_stream_error_line_is_reported_as_a_stream_error() -> None:
    adapter = client(
        lambda _request: httpx.Response(200, content=ndjson([{"error": "model not found"}]))
    )

    with pytest.raises(ModelProviderError) as raised:
        [event async for event in adapter.stream_events(request_for())]
    await adapter.close()

    # The host's own sentence, not a generic "stream error": when a window
    # cannot be allocated this line is the only thing that says why.
    assert "model not found" in str(raised.value)


async def test_a_stream_that_stops_early_never_completes_a_response() -> None:
    """Half a stream is not an answer: tools must not be dispatched from one."""
    adapter = client(
        lambda _request: httpx.Response(200, content=ndjson([{"message": {"content": "half"}}]))
    )

    with pytest.raises(ModelProviderError):
        [event async for event in adapter.stream_events(request_for())]
    await adapter.close()


async def test_chat_allows_for_the_reload_a_changed_window_causes() -> None:
    """Two budgets in one: a long answer must not die on the 30 s read budget
    that sizes a metadata call, and a chat that changes the effective
    ``num_ctx`` reloads a multi-GB runner before the first token — the same
    wait ``load_model`` already budgets 600 s for."""
    adapter = client(lambda _request: httpx.Response(200, json=done()))

    # Just under the load budget, so the adapter -- which can quote the host's
    # own complaint -- expires before the reasoning activity's ten minutes and
    # gets to be the one that explains the failure.
    assert adapter._chat_timeout.read == (_LOAD_TIMEOUT.read or 0.0) - 30.0
    assert adapter._chat_timeout.read < (_LOAD_TIMEOUT.read or 0.0)
    assert adapter._chat_timeout.connect == 10.0
    await adapter.close()


async def test_a_non_object_options_extra_never_displaces_the_pinned_window() -> None:
    """``extra`` outranks what the adapter fills in, but only as a map of
    options. A scalar cannot merge, and letting it through replaced the whole
    block -- temperature, num_predict, and the num_ctx the prompt was just
    budgeted against -- with the scalar itself."""
    adapter, seen = recording(httpx.Response(200, json=done()))

    with pytest.raises(ModelProviderError) as raised:
        await adapter.generate(request_for(extra={"options": "num_ctx=131072"}))
    await adapter.close()

    assert raised.value.retryable is False
    assert "body" not in seen


async def test_a_two_hundred_carrying_an_error_is_not_an_empty_answer() -> None:
    """Ollama answers an unallocatable request 200 with an ``error`` key. Read
    as a normal body that is an empty completion, and the host's diagnostic --
    the one sentence that explains a window that would not load -- is gone."""
    memory = "model requires more system memory (18.0 GiB) than is available (12.4 GiB)"
    adapter = client(lambda _request: httpx.Response(200, json={"error": memory}))

    with pytest.raises(ModelProviderError) as raised:
        await adapter.generate(request_for())
    await adapter.close()

    assert memory in str(raised.value)


async def test_a_streamed_call_without_a_name_is_dropped_like_a_buffered_one() -> None:
    """Both paths meet the same malformed call: the buffered one ignores it
    rather than guessing, and the streamed one used to fail the whole step
    non-retryably for it."""
    adapter = client(
        lambda _request: httpx.Response(
            200,
            content=ndjson(
                [
                    {"message": {"tool_calls": [{"id": "call_1", "function": {"arguments": {}}}]}},
                    done(),
                ]
            ),
        )
    )

    events = [event async for event in adapter.stream_events(request_for())]
    await adapter.close()

    completed = events[-1]
    assert completed.response is not None
    assert completed.response.tool_calls == ()


async def test_a_streamed_call_whose_function_is_not_an_object_is_dropped() -> None:
    """``function`` typed as a string raised AttributeError straight out of the
    async generator, where no provider-error classification could reach it."""
    adapter = client(
        lambda _request: httpx.Response(
            200,
            content=ndjson(
                [
                    {"message": {"tool_calls": [{"id": "call_1", "function": "search"}]}},
                    {"message": {"tool_calls": [NATIVE_CALL]}},
                    done(),
                ]
            ),
        )
    )

    events = [event async for event in adapter.stream_events(request_for())]
    await adapter.close()

    completed = events[-1]
    assert completed.response is not None
    # The malformed line is dropped; the real call keeps its own identity and
    # does not inherit the dropped one's slot.
    assert [call.id for call in completed.response.tool_calls] == ["call_3rq2qq9d"]
    assert completed.response.tool_calls[0].name == "ghost.archive.search"
