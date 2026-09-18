"""What Jhin asks Ollama to serve, and what it may learn about that.

``num_ctx`` is in the name because it is now sent. Measured against Ollama
0.34.1, the identical ``{"options": {"num_ctx": 8192}}`` loads the model at
8192 through native ``/api/chat`` and at the host's own 32768 through
``/v1/chat/completions``, which drops the option — which is why chat speaks
the native route at all. ``/api/ps`` still reports what a resident instance is
really being served with, so a host that answered an ask for more with less
clamps the budget instead of silently shifting the prompt.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest, ToolSchema
from jhin_models.providers.ollama import (
    OllamaClient,
    OllamaUnsupported,
    measured_context_window,
    requested_serving_window,
    serving_window_options,
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


def ps_body(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"models": rows}


def loaded(name: str, context_length: int | None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "model": name, "size": 18_000_000_000}
    if context_length is not None:
        row["context_length"] = context_length
    return row


def transport(ps: httpx.Response, seen: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            if seen is not None:
                seen["probed"] = seen.get("probed", 0) + 1
            return ps
        if seen is not None:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response())

    return httpx.MockTransport(handler)


async def sent_body(extra: dict[str, Any], tools: tuple[ToolSchema, ...] = ()) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    client = OllamaClient(transport=transport(httpx.Response(200, json=ps_body([])), seen))
    await client.generate(
        ModelRequest(
            model="qwen3.8:latest",
            messages=(ModelMessage(role="user", content="hi"),),
            max_output_tokens=512,
            temperature=0.3,
            tools=tools,
            extra=extra,
        )
    )
    await client.close()
    assert seen["path"] == "/api/chat"
    return dict(seen["body"])


async def test_the_requested_window_reaches_the_wire_as_num_ctx() -> None:
    """The point of the native route: this option is honoured here and was
    dropped on ``/v1``, so the window Jhin budgets against is the window the
    instance is loaded with."""
    body = await sent_body(serving_window_options(65_536))

    assert body["options"]["num_ctx"] == 65_536


def test_only_a_configured_profile_window_is_ever_asked_for() -> None:
    """A window nobody configured is not a window anybody chose. Asking for the
    documented default would still be an allocation, and would shrink — and
    reload — a host deliberately started with a larger OLLAMA_CONTEXT_LENGTH,
    or an instance another client loaded larger."""
    assert requested_serving_window(provider_type="ollama", context_window=131_072) == 131_072
    assert requested_serving_window(provider_type="ollama", context_window=None) is None
    assert requested_serving_window(provider_type="ollama", context_window=0) is None
    # Every other provider serves what it serves; there is nothing to ask for.
    assert (
        requested_serving_window(provider_type="openai_compatible", context_window=128_000) is None
    )


def test_the_configured_window_is_not_capped() -> None:
    """qwen3.8's architecture allows 262,144 and the reference card cannot hold
    it. A cap here would quietly budget the prompt against a smaller window than
    the operator configured; the host refuses what it cannot allocate, and says
    so in a sentence the adapter now surfaces."""
    assert requested_serving_window(provider_type="ollama", context_window=262_144) == 262_144


async def test_no_requested_window_pins_nothing() -> None:
    # Nothing to say, nothing said: the host keeps whatever window it was
    # started with rather than being shrunk to a number nobody chose.
    assert serving_window_options(None) == {}
    assert serving_window_options(0) == {}
    assert "num_ctx" not in (await sent_body({}))["options"]


async def test_the_output_limit_and_temperature_travel_as_native_options() -> None:
    body = await sent_body(serving_window_options(32_768))

    assert body["options"] == {"temperature": 0.3, "num_predict": 512, "num_ctx": 32_768}


async def test_profile_options_still_travel_alongside_the_window() -> None:
    """A caller's own Ollama options are not displaced by the pin, and still
    outrank what the adapter filled in by itself."""
    body = await sent_body({"options": {"num_ctx": 8_192, "seed": 7, "temperature": 0}})

    assert body["options"] == {"temperature": 0, "num_predict": 512, "num_ctx": 8_192, "seed": 7}


async def test_measured_window_is_the_resident_context_length() -> None:
    client = OllamaClient(
        transport=transport(httpx.Response(200, json=ps_body([loaded("qwen3.8:latest", 32_768)])))
    )

    measured = await measured_context_window(client, provider_type="ollama", model="qwen3.8:latest")
    await client.close()

    assert measured == 32_768


async def test_measured_window_matches_an_untagged_model_name() -> None:
    client = OllamaClient(
        transport=transport(httpx.Response(200, json=ps_body([loaded("qwen3.8:latest", 8_192)])))
    )

    measured = await measured_context_window(client, provider_type="ollama", model="qwen3.8")
    await client.close()

    assert measured == 8_192


async def test_another_models_window_is_never_borrowed() -> None:
    client = OllamaClient(
        transport=transport(httpx.Response(200, json=ps_body([loaded("llama3.1:70b", 131_072)])))
    )

    measured = await measured_context_window(client, provider_type="ollama", model="qwen3.8:latest")
    await client.close()

    # Nothing is measured for a model that is not resident; a neighbour's
    # roomier window is not evidence about this one.
    assert measured is None


async def test_a_failed_probe_measures_nothing_and_raises_nothing() -> None:
    client = OllamaClient(transport=transport(httpx.Response(500, text="boom")))

    measured = await measured_context_window(client, provider_type="ollama", model="qwen3.8:latest")
    await client.close()

    assert measured is None


async def test_a_failed_probe_does_not_hide_a_real_management_error() -> None:
    """The tolerance belongs to the budget probe alone: asking the host
    directly still reports the failure."""
    client = OllamaClient(transport=transport(httpx.Response(500, text="boom")))

    with pytest.raises(ModelProviderError):
        await client.loaded_models()
    await client.close()


async def test_a_non_ollama_provider_is_never_probed() -> None:
    seen: dict[str, Any] = {}
    client = OllamaClient(
        transport=transport(
            httpx.Response(200, json=ps_body([loaded("qwen3.8:latest", 32_768)])), seen
        )
    )

    measured = await measured_context_window(
        client, provider_type="openai_compatible", model="gpt-4o-mini"
    )
    await client.close()

    assert measured is None
    assert "probed" not in seen


async def test_a_client_that_cannot_reach_the_native_api_measures_nothing() -> None:
    class NotAnOllamaClient:
        provider_name = "openai_compatible"

    measured = await measured_context_window(
        NotAnOllamaClient(), provider_type="ollama", model="qwen3.8:latest"
    )

    assert measured is None


async def test_a_profile_mislabelled_ollama_leaves_the_budget_unmeasured() -> None:
    """A profile may name the ollama provider type over any client at all, and
    a telemetry wrapper answers that mismatch by raising. Measuring nothing is
    the answer; failing the step for want of an optional number is not."""

    class InstrumentedNonOllama:
        provider_name = "openai_compatible"

        def ollama_client(self) -> object:
            raise OllamaUnsupported("openai_compatible: local model management needs an Ollama")

    measured = await measured_context_window(
        InstrumentedNonOllama(), provider_type="ollama", model="qwen3.8:latest"
    )

    assert measured is None


async def test_an_instrumented_ollama_client_is_still_measurable() -> None:
    """The wrapper Jhin puts around a real adapter must not hide the probe."""
    inner = OllamaClient(
        transport=transport(httpx.Response(200, json=ps_body([loaded("qwen3.8:latest", 32_768)])))
    )

    class Instrumented:
        provider_name = "ollama"

        def ollama_client(self) -> OllamaClient:
            return inner

    measured = await measured_context_window(
        Instrumented(), provider_type="ollama", model="qwen3.8:latest"
    )
    await inner.close()

    assert measured == 32_768
