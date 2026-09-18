"""The fake provider must behave like a real OpenAI-compatible endpoint
through our own adapter — otherwise it proves nothing (plan 32.2)."""

import json

import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest, build_model_client
from jhin_models.testing import FakeOpenAIServer
from jhin_models.testing.fake_openai import (
    FAIL_MODEL,
    build_completion,
    completion_latency_seconds,
    encode_marker_payload,
)


@pytest.mark.parametrize("system_tools_only", [False, True])
def test_reviewer_keeps_own_script_across_two_review_cycles(system_tools_only: bool) -> None:
    """QA's fix advice belongs to the SWE, including when a retest quotes it."""
    fix = '[[tool:cli.file.edit {"path":"app.py","old_text":"bug","new_text":"fix"}]]'
    report = json.dumps(
        {
            "verdict": "__VERDICT__",
            "summary": "Suggested fix: " + encode_marker_payload(encode_marker_payload(fix)),
        }
    )
    qa_script = (
        ("[[system_tools_only]] " if system_tools_only else "")
        + '[[tool:cli.command.run {"command":"pytest"}]] '
        + f"[[tool:coordination.report_review {report}]]"
    )
    previous_summary = ""
    for cycle in (1, 2):
        messages = [
            {"role": "system", "content": "Platform instructions."},
            {"role": "system", "content": qa_script},
            {"role": "user", "content": f"Review cycle {cycle}. {previous_summary}"},
        ]
        _, response = build_completion({"messages": messages})
        call = response["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "cli.command.run"
        messages.append({"role": "tool", "content": json.dumps({"exit_code": 2 - cycle})})
        _, response = build_completion({"messages": messages})
        call = response["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "coordination.report_review"
        arguments = json.loads(call["function"]["arguments"])
        assert arguments["verdict"] == ("fail" if cycle == 1 else "pass")
        previous_summary = arguments["summary"]
        messages.append({"role": "tool", "content": '{"status":"reported"}'})
        _, response = build_completion({"messages": messages})
        choice = response["choices"][0]
        if system_tools_only or cycle == 1:
            assert choice["finish_reason"] == "stop"
            assert "tool_calls" not in choice["message"]
        else:
            # Without the opt-in, the existing all-message script semantics
            # deliberately remain intact, exposing why QA needs the directive.
            assert choice["message"]["tool_calls"][0]["function"]["name"] == "cli.file.edit"

        # The default SWE consumes the report's once-encoded advice on both
        # cycles; limiting QA must never globally disable user-script markers.
        _, response = build_completion(
            {
                "messages": [
                    {"role": "system", "content": "Implement the suggested fix."},
                    {"role": "user", "content": previous_summary},
                ]
            }
        )
        call = response["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "cli.file.edit"
        assert json.loads(call["function"]["arguments"])["new_text"] == "fix"


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_system_tools_only_directive_has_no_effect_outside_system(role: str) -> None:
    _, response = build_completion(
        {
            "messages": [
                {"role": "system", "content": "Use tools when instructed."},
                {"role": role, "content": "[[system_tools_only]]"},
                {"role": "user", "content": '[[tool:system.echo {"text":"still scripted"}]]'},
            ]
        }
    )
    call = response["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "system.echo"


def test_system_tools_only_preserves_normal_echo() -> None:
    _, response = build_completion(
        {
            "model": "fake-mini",
            "messages": [
                {"role": "system", "content": "[[system_tools_only]]"},
                {"role": "user", "content": "Plain request without tool markers"},
            ],
        }
    )
    assert response["choices"][0]["message"]["content"] == (
        "[fake-mini] Completed: Plain request without tool markers"
    )


@pytest.mark.parametrize("opt_in", [False, True])
@pytest.mark.parametrize("latest", ["First request", "Second request"])
def test_opt_in_chat_echo_preserves_latest_request_through_evidence_review(
    opt_in: bool, latest: str
) -> None:
    review = (
        "You have not received a tool result for the latest request. Re-evaluate "
        "the person's original request before publishing the draft below."
    )
    messages = [
        {"role": "system", "content": "[[echo_latest_user]]" if opt_in else "Echo"},
        {"role": "user", "content": "Older request"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": latest},
        {"role": "user", "content": review},
    ]
    status, response = build_completion({"model": "fake-mini", "messages": messages})
    assert status == 200
    expected = latest if opt_in else review
    assert response["choices"][0]["message"]["content"] == f"[fake-mini] Completed: {expected}"
    # Ordinary later user turns still replace the previous request, including
    # after a review; the marker never selects a fixed earlier turn.
    messages.append({"role": "user", "content": "New follow-up"})
    _, response = build_completion({"model": "fake-mini", "messages": messages})
    assert response["choices"][0]["message"]["content"] == "[fake-mini] Completed: New follow-up"


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


@pytest.mark.parametrize(
    "instruction",
    ["Say hello to Jhin", '[[tool:system.echo {"text":"' + "héllo " * 40 + '"}]]'],
)
async def test_typed_stream_roundtrip_matches_nonstream_response(instruction: str) -> None:
    request = ModelRequest(
        model="fake-mini", messages=(ModelMessage(role="user", content=instruction),)
    )
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            expected = await client.generate(request)
            events = [event async for event in client.stream_events(request)]
        finally:
            await client.close()
    assert events[-1].type == "completed"
    actual = events[-1].response
    assert actual is not None
    assert actual.text == expected.text
    assert actual.tool_calls == expected.tool_calls
    assert actual.usage == expected.usage
    assert actual.finish_reason == expected.finish_reason
    assert actual.provider_request_id == expected.provider_request_id
    if expected.tool_calls:
        assert len([event for event in events if event.type == "tool_delta"]) > 1


async def test_streaming_provider_failure_keeps_http_error_classification() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            with pytest.raises(ModelProviderError) as failure:
                _ = [
                    event
                    async for event in client.stream_events(
                        ModelRequest(
                            model=FAIL_MODEL, messages=(ModelMessage(role="user", content="fail"),)
                        )
                    )
                ]
        finally:
            await client.close()
    assert failure.value.status_code == 500
    assert failure.value.retryable


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
    reply = envelope["choices"][0]["message"]["content"]
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
    reply = envelope["choices"][0]["message"]["content"]
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
    reply = envelope["choices"][0]["message"]["content"]
    document = json.loads(reply)
    assert document["candidates"][0]["content"] == "CTO delegated: Write the deploy docs"


def test_ordinary_chat_is_not_intercepted() -> None:
    status, envelope = build_completion(
        {"model": "fake-mini", "messages": [{"role": "user", "content": "remember me?"}]}
    )
    assert status == 200
    reply = envelope["choices"][0]["message"]["content"]
    assert reply.startswith("[fake-mini] Completed:")


# --- deterministic dedup adjudication (jhin_memory.adjudication contract) ---

_ADJUDICATION_SYSTEM = (
    "You compare pairs of remembered statements from one workspace and decide "
    "whether the two statements in each pair record the SAME real-world fact."
)


def _adjudication_body(pairs: list[tuple[str, str]]) -> dict[str, object]:
    lines: list[str] = []
    for index, (a, b) in enumerate(pairs, start=1):
        lines.append(f"Pair {index} (subjects: - | -)")
        lines.append(f"A: {a}")
        lines.append(f"B: {b}")
    user = "Decide SAME or DIFFERENT for each pair.\n\n" + "\n".join(lines)
    return {
        "model": "fake-mini",
        "messages": [
            {"role": "system", "content": _ADJUDICATION_SYSTEM},
            {"role": "user", "content": user},
        ],
    }


def test_adjudication_shares_value_token_means_same() -> None:
    status, payload = build_completion(
        _adjudication_body(
            [
                ("We deploy every other Thursday.", "The release day is every other Thursday."),
                ("We deploy every other Thursday.", "We deploy every Friday."),
                ("The office is closed.", "The kitchen is closed."),
            ]
        )
    )
    assert status == 200
    reply = json.loads(payload["choices"][0]["message"]["content"])
    # Same weekday → SAME; conflicting weekday → DIFFERENT; no value tokens
    # at all → DIFFERENT (never merge on doubt).
    assert reply == {"verdicts": ["SAME", "DIFFERENT", "DIFFERENT"]}


def test_adjudication_numbers_count_as_value_tokens() -> None:
    status, payload = build_completion(
        _adjudication_body(
            [
                ("The retry limit is 3.", "We retry at most 3 times."),
                ("The retry limit is 3.", "The retry limit is 5."),
            ]
        )
    )
    assert status == 200
    reply = json.loads(payload["choices"][0]["message"]["content"])
    assert reply == {"verdicts": ["SAME", "DIFFERENT"]}


def test_completion_latency_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var (or a junk value) must not slow the pytest fixture down."""
    monkeypatch.delenv("FAKE_PROVIDER_LATENCY_MS", raising=False)
    assert completion_latency_seconds() == 0.0

    monkeypatch.setenv("FAKE_PROVIDER_LATENCY_MS", "not-a-number")
    assert completion_latency_seconds() == 0.0

    monkeypatch.setenv("FAKE_PROVIDER_LATENCY_MS", "-500")
    assert completion_latency_seconds() == 0.0


def test_completion_latency_reads_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """QA slows the compose service down to exercise the mid-run controls."""
    monkeypatch.setenv("FAKE_PROVIDER_LATENCY_MS", "2500")
    assert completion_latency_seconds() == 2.5
