"""Prompt budgeting: what may be shortened, what must fit, what is reserved.

The wire assertions use a real :class:`OllamaClient` over an httpx
MockTransport, because the number the budget admits against has to come from
the server rather than from the catalogue: a profile's ``context_window`` is
the model architecture's maximum, and the host may be serving a fraction of
it. Nothing here talks to a real Ollama.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from jhin_agents.context import UNTRUSTED_LABEL, TaskContext
from jhin_agents.context_budget import (
    DEFAULT_OLLAMA_CONTEXT_WINDOW,
    DEFAULT_OUTPUT_TOKENS,
    budget_context,
    requested_context_window,
    resolve_context_window,
)
from jhin_agents.runtime import execute_step
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_models import ModelMessage, ModelProviderError, ToolSchema
from jhin_models.providers.ollama import OllamaClient

WINDOW = 32_768


def conversation(size: int) -> tuple[ModelMessage, ...]:
    return (
        ModelMessage(role="system", content="S" * 400),
        ModelMessage(role="user", content="U" * 400),
        ModelMessage(role="tool", content=UNTRUSTED_LABEL + "T" * size, tool_call_id="call-1"),
        ModelMessage(role="assistant", content="A" * size),
    )


def test_shortening_never_touches_system_or_user() -> None:
    # Large enough that shortening the tool result alone cannot get under the
    # limit, so the assistant turn has to shrink too.
    messages = conversation(150_000)
    result = budget_context(
        messages,
        (),
        provider_type="ollama",
        context_window=WINDOW,
        max_output_tokens=1024,
    )

    assert result.messages[0] == messages[0]
    assert result.messages[1] == messages[1]
    assert result.shortened_messages == 2
    for original, kept in zip(messages[2:], result.messages[2:], strict=True):
        assert len(kept.content) < len(original.content)
        assert kept.role == original.role
        assert kept.tool_call_id == original.tool_call_id


def test_shortened_content_says_the_excerpt_is_incomplete() -> None:
    result = budget_context(
        conversation(150_000),
        (),
        provider_type="ollama",
        context_window=WINDOW,
        max_output_tokens=1024,
    )

    tool, assistant = result.messages[2], result.messages[3]
    assert tool.content.startswith(UNTRUSTED_LABEL)
    assert "incomplete" in tool.content
    assert "call-1" in tool.content
    assert "incomplete" in assistant.content


def test_protected_content_that_cannot_fit_is_refused_not_sent() -> None:
    messages = (
        ModelMessage(role="system", content="S" * 400),
        ModelMessage(role="user", content="U" * 200_000),
    )

    with pytest.raises(ModelProviderError) as raised:
        budget_context(
            messages,
            (),
            provider_type="ollama",
            context_window=WINDOW,
            max_output_tokens=1024,
        )

    assert raised.value.error_code == "model_context_budget_exceeded"
    assert raised.value.retryable is False


def test_output_space_is_reserved_inside_the_window() -> None:
    result = budget_context(
        conversation(60_000),
        (ToolSchema(name="files.read", description="Read a file"),),
        provider_type="ollama",
        context_window=WINDOW,
        max_output_tokens=2000,
    )

    assert result.max_output_tokens == 2000
    assert result.input_token_limit is not None
    assert result.estimated_input_tokens is not None
    assert result.input_token_limit + result.max_output_tokens <= WINDOW
    assert result.estimated_input_tokens <= result.input_token_limit


def test_unknown_window_invents_neither_a_window_nor_an_output_cap() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="anthropic",
        context_window=None,
        max_output_tokens=None,
    )

    # A profile with no recorded window is not evidence of a small one. An
    # invented 4096 cap here would truncate answers that used to finish, and a
    # truncated answer is rejected as an incomplete task, non-retryably.
    assert result.context_window is None
    assert result.max_output_tokens is None


def test_a_hosted_window_reserves_output_room_without_capping_the_answer() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="anthropic",
        context_window=200_000,
        max_output_tokens=None,
    )

    # The admission limit still keeps DEFAULT_OUTPUT_TOKENS of room, but the
    # provider's own output default is what actually bounds the answer: a
    # hosted API refuses an oversized request rather than silently dropping
    # the oldest tokens, so Jhin has no window to enforce from the outside.
    assert result.max_output_tokens is None
    assert result.input_token_limit == 200_000 - DEFAULT_OUTPUT_TOKENS - 200_000 // 16


def test_a_configured_output_limit_is_always_the_one_sent() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="anthropic",
        context_window=200_000,
        max_output_tokens=777,
    )

    assert result.max_output_tokens == 777


def test_unmeasured_ollama_falls_back_to_the_documented_assumption() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=None,
        max_output_tokens=512,
    )

    assert result.context_window == DEFAULT_OLLAMA_CONTEXT_WINDOW
    # Named an assumption, not a measurement: nothing asked the host.
    assert result.context_window_source == "assumed"
    assert result.input_token_limit is not None
    assert result.input_token_limit + 512 <= DEFAULT_OLLAMA_CONTEXT_WINDOW


def test_the_served_window_clamps_an_architecture_maximum() -> None:
    """qwen3.8's architecture allows 262,144; the resident instance serves
    32,768 on a box where one such model fills the card. The budget admits
    against what is served."""
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=262_144,
        max_output_tokens=512,
        served_context_window=32_768,
    )

    assert result.context_window == 32_768
    assert result.context_window_source == "measured"
    assert result.input_token_limit is not None
    assert result.input_token_limit + 512 <= 32_768


def test_a_smaller_profile_window_still_wins_over_a_larger_measurement() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=8_192,
        max_output_tokens=512,
        served_context_window=32_768,
    )

    # The smallest credible window, from either side: the measurement is a
    # ceiling, never a licence to admit more than the profile allows.
    assert result.context_window == 8_192
    assert result.context_window_source == "profile"


def test_a_profile_window_above_the_default_is_asked_for_and_budgeted_against() -> None:
    """The reference host serves 131,072 fully on the GPU. Before Jhin could
    set the window this had to be clamped to the 32,768 default, which is what
    refused a prompt the card had room for twice over."""
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=131_072,
        max_output_tokens=4096,
    )

    assert result.requested_context_window == 131_072
    assert (result.context_window, result.context_window_source) == (131_072, "profile")
    assert result.input_token_limit == 131_072 - 4096 - 131_072 // 16


def test_a_smaller_measurement_clamps_the_budget_without_lowering_the_ask() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=131_072,
        max_output_tokens=4096,
        served_context_window=32_768,
    )

    # Later steps budget against what the host actually served ...
    assert (result.context_window, result.context_window_source) == (32_768, "measured")
    # ... while still asking for the window the profile chose, so a host with
    # room for it serves it rather than being talked down by its own history.
    assert result.requested_context_window == 131_072


@pytest.mark.parametrize(
    ("provider_type", "profile", "served", "expected"),
    [
        ("ollama", 262_144, 32_768, (32_768, "measured")),
        ("ollama", 8_192, 32_768, (8_192, "profile")),
        ("ollama", None, 4_096, (4_096, "measured")),
        ("ollama", None, None, (DEFAULT_OLLAMA_CONTEXT_WINDOW, "assumed")),
        ("ollama", 32_768, 32_768, (32_768, "measured")),
        # Nothing measured: the profile's number is what Jhin asks for, so it
        # is also what Jhin budgets against -- above the default included.
        ("ollama", 131_072, None, (131_072, "profile")),
        ("anthropic", None, None, (None, None)),
        ("anthropic", 200_000, None, (200_000, "profile")),
        # A nonsense report is not a window; it must not become one.
        ("ollama", 262_144, 0, (262_144, "profile")),
        ("ollama", 0, None, (DEFAULT_OLLAMA_CONTEXT_WINDOW, "assumed")),
    ],
)
def test_resolve_context_window_takes_the_smallest_credible_number(
    provider_type: str, profile: int | None, served: int | None, expected: tuple[int | None, str]
) -> None:
    assert (
        resolve_context_window(
            provider_type=provider_type, context_window=profile, served_context_window=served
        )
        == expected
    )


def chat_response() -> dict[str, Any]:
    return {
        "model": "qwen3.8:latest",
        "created_at": "2026-09-02T10:00:00Z",
        "message": {"role": "assistant", "content": "Done."},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 10,
        "eval_count": 2,
    }


def ollama_snapshot(context_window: int | None) -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot(
        agent_id=uuid4(),
        workspace_id=uuid4(),
        name="Senior SWE",
        role_title="Senior Software Engineer",
        system_prompt="You write production-quality software.",
        autonomy_level="supervised",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        model_profile=ModelProfileSnapshot(
            profile_id=uuid4(),
            provider_id=uuid4(),
            provider_type="ollama",
            base_url="http://ollama.local:11434/v1",
            secret_id=None,
            model_name="qwen3.8:latest",
            display_name="Qwen 3.8",
            input_cost_micros_per_million=0,
            output_cost_micros_per_million=0,
            context_window=context_window,
        ),
        temperature=0.3,
        max_output_tokens=512,
        run_limits=RunLimits(max_steps=5, max_run_minutes=10),
    )


def ollama_transport(ps: httpx.Response, seen: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            seen["probed"] = seen.get("probed", 0) + 1
            return ps
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    return httpx.MockTransport(handler)


def resident(name: str, context_length: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "models": [
                {
                    "name": name,
                    "model": name,
                    "size": 18_000_000_000,
                    "size_vram": 17_000_000_000,
                    "context_length": context_length,
                }
            ]
        },
    )


@pytest.mark.parametrize(
    ("context_window", "ps", "admitted", "requested"),
    [
        # A profile asking for more than the host is serving: the request
        # stands (the instance reloads at that size), the budget does not.
        (262_144, resident("qwen3.8:latest", 32_768), 32_768, 262_144),
        # No configured window and nothing resident to measure: nothing is
        # asked for, and the documented default is admitted as an assumption.
        (None, httpx.Response(200, json={"models": []}), DEFAULT_OLLAMA_CONTEXT_WINDOW, None),
        # The host cannot be asked at all; the profile's own number stands.
        (131_072, httpx.Response(500, text="boom"), 131_072, 131_072),
        # Nothing asked for, so what the host reports serving is simply true.
        (None, resident("qwen3.8:latest", 16_384), 16_384, None),
    ],
)
async def test_the_window_is_asked_for_and_then_clamped_by_what_is_served(
    context_window: int | None, ps: httpx.Response, admitted: int, requested: int | None
) -> None:
    seen: dict[str, Any] = {}
    client = OllamaClient(
        base_url="http://ollama.local:11434/v1", transport=ollama_transport(ps, seen)
    )
    await execute_step(
        client, ollama_snapshot(context_window), TaskContext(title="Do it", description="")
    )
    await client.close()

    assert seen["probed"] == 1
    # 512 is the snapshot's configured output limit; the reserve keeps it and
    # the margin inside whatever window was admitted.
    assert seen["body"]["options"]["num_predict"] == 512
    assert 512 + max(1024, admitted // 16) <= admitted
    # The window the instance is loaded with, pinned on the wire that honours
    # it. A measurement may lower what Jhin admits without lowering the ask;
    # an unconfigured profile pins nothing at all and leaves the host its own.
    assert seen["body"]["options"].get("num_ctx") == requested


async def test_a_measured_window_is_recorded_as_measured_not_assumed() -> None:
    seen: dict[str, Any] = {}
    client = OllamaClient(
        base_url="http://ollama.local:11434/v1",
        transport=ollama_transport(resident("qwen3.8:latest", 32_768), seen),
    )
    outcome = await execute_step(
        client, ollama_snapshot(262_144), TaskContext(title="Do it", description="")
    )
    await client.close()

    detail = outcome.transitions[0].detail
    assert "context window 32768 (measured)" in detail
    # And what was asked for, without which a clamp cannot be told from a
    # profile that was always this size.
    assert "requested num_ctx 262144" in detail


async def test_an_unmeasurable_window_is_recorded_as_an_assumption() -> None:
    seen: dict[str, Any] = {}
    client = OllamaClient(
        base_url="http://ollama.local:11434/v1",
        transport=ollama_transport(httpx.Response(500, text="boom"), seen),
    )
    outcome = await execute_step(
        client, ollama_snapshot(None), TaskContext(title="Do it", description="")
    )
    await client.close()

    assert (
        f"context window {DEFAULT_OLLAMA_CONTEXT_WINDOW} (assumed)" in outcome.transitions[0].detail
    )


def test_a_configured_window_is_both_asked_for_and_budgeted_against() -> None:
    # Jhin sends num_ctx now, so the profile's number is not a claim about the
    # host any more: it is the instruction the host is loaded from. Cold, with
    # /api/ps empty, that instruction is the honest thing to budget against.
    window, source = resolve_context_window(
        provider_type="ollama", context_window=131_072, served_context_window=None
    )
    assert (window, source) == (131_072, "profile")
    assert requested_context_window(provider_type="ollama", context_window=131_072) == 131_072


def test_an_unconfigured_profile_asks_for_nothing_and_assumes_the_default() -> None:
    # Two different claims. Nothing is asked for, because num_ctx is an
    # allocation and a host running larger by its own configuration must not be
    # shrunk to a number nobody chose; the default is still what an unmeasured
    # budget admits against, named as the assumption it is.
    assert requested_context_window(provider_type="ollama", context_window=None) is None
    assert resolve_context_window(
        provider_type="ollama", context_window=None, served_context_window=None
    ) == (DEFAULT_OLLAMA_CONTEXT_WINDOW, "assumed")


def test_a_provider_whose_window_jhin_cannot_set_is_asked_for_nothing() -> None:
    assert requested_context_window(provider_type="anthropic", context_window=200_000) is None


def test_an_unmeasured_profile_smaller_than_the_assumption_still_wins() -> None:
    window, source = resolve_context_window(
        provider_type="ollama", context_window=8_192, served_context_window=None
    )
    assert (window, source) == (8_192, "profile")


def test_a_measurement_may_legitimately_exceed_the_assumption() -> None:
    window, source = resolve_context_window(
        provider_type="ollama", context_window=262_144, served_context_window=131_072
    )
    assert (window, source) == (131_072, "measured")


def test_an_unconfigured_profile_pins_no_window_at_all() -> None:
    """Before Jhin sent ``num_ctx`` a profile with no window sent nothing, and
    a host started with a larger ``OLLAMA_CONTEXT_LENGTH`` — or an instance
    another client loaded larger — kept the window it had. Pinning the assumed
    default would shrink that host, and reload its runner, to a number nobody
    chose."""
    assert requested_context_window(provider_type="ollama", context_window=None) is None


def test_an_unconfigured_profile_budgets_against_what_the_host_serves() -> None:
    result = budget_context(
        conversation(400),
        (),
        provider_type="ollama",
        context_window=None,
        max_output_tokens=512,
        served_context_window=131_072,
    )

    # Nothing was asked for, so the measurement is the whole truth about the
    # window; the assumed default is the fallback for when nothing is measured,
    # not a ceiling over a host that is demonstrably serving more.
    assert (result.context_window, result.context_window_source) == (131_072, "measured")
    assert result.requested_context_window is None


async def test_an_unconfigured_profile_sends_no_num_ctx_on_the_wire() -> None:
    seen: dict[str, Any] = {}
    client = OllamaClient(
        base_url="http://ollama.local:11434/v1",
        transport=ollama_transport(resident("qwen3.8:latest", 131_072), seen),
    )
    outcome = await execute_step(
        client, ollama_snapshot(None), TaskContext(title="Do it", description="")
    )
    await client.close()

    assert "num_ctx" not in seen["body"]["options"]
    detail = outcome.transitions[0].detail
    assert "context window 131072 (measured)" in detail
    # Nothing was requested, so nothing is claimed to have been.
    assert "requested num_ctx" not in detail
