"""Ollama's native model management against an httpx MockTransport.

One handler serves both the OpenAI-compatible ``/v1`` routes and the native
``/api`` routes, because the adapter drives both from one provider base URL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jhin_models import ModelMessage, ModelProviderError, ModelRequest
from jhin_models.providers.ollama import (
    DEFAULT_KEEP_ALIVE,
    KEEP_ALIVE_FOREVER,
    KEEP_ALIVE_UNLOAD,
    OllamaClient,
    OllamaNativeClient,
    OllamaUnsupported,
    as_ollama_client,
    native_origin,
    validate_keep_alive,
)
from jhin_models.providers.openai_compatible import OpenAICompatibleClient

TAGS: dict[str, Any] = {
    "models": [
        {
            "name": "qwen3.8:latest",
            "model": "qwen3.8:latest",
            "modified_at": "2026-08-30T12:00:00Z",
            "size": 17700000000,
            "digest": "def",
            "details": {
                "family": "qwen3",
                "parameter_size": "27.3B",
                "quantization_level": "Q4_K_M",
            },
        },
        {
            "name": "muse-glimmer:latest",
            "model": "muse-glimmer:latest",
            "modified_at": "2026-08-30T12:34:56.123456789-07:00",
            "size": 18200000000,
            "digest": "abc",
            "details": {
                "family": "llama",
                "families": ["llama"],
                "parameter_size": "27.9B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        # Older manifests and hand-built models can miss every optional field.
        {"name": "bare:latest"},
        # Rows without a usable name are skipped, never guessed at.
        {"model": "nameless", "size": 5},
        {"name": "   ", "size": 5},
        "not-an-object",
    ]
}

PS: dict[str, Any] = {
    "models": [
        {
            "name": "muse-glimmer:latest",
            "model": "muse-glimmer:latest",
            "size": 18200000000,
            "size_vram": 18200000000,
            "expires_at": "2026-09-02T10:05:00Z",
            "context_length": 8192,
            "details": {"family": "llama"},
        }
    ]
}

SHOW: dict[str, Any] = {
    "license": "Apache License 2.0\n                           Version 2.0, January 2004\n...",
    "details": {"family": "qwen3", "parameter_size": "27.3B", "quantization_level": "Q4_K_M"},
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
    "capabilities": ["completion", "tools", "thinking"],
}

NOT_FOUND = {"error": "model 'nope:latest' not found, try pulling it first"}


def _chat_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": "qwen3.8:latest",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _handler(
    seen: list[httpx.Request],
    *,
    tags: Any = TAGS,
    ps: Any = PS,
    show: Any = SHOW,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json=tags)
        if path == "/api/ps":
            return httpx.Response(200, json=ps)
        if path == "/api/show":
            return httpx.Response(200, json=show)
        if path == "/api/generate":
            body = json.loads(request.content)
            if body["model"] == "nope:latest":
                return httpx.Response(404, json=NOT_FOUND)
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "created_at": "2026-09-02T10:00:00Z",
                    "response": "",
                    "done": True,
                    "done_reason": "unload" if body["keep_alive"] == 0 else "load",
                },
            )
        if path == "/v1/chat/completions":
            return httpx.Response(200, json=_chat_response())
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return handler


def _client(
    handler: Any, *, base_url: str = "http://fake/v1", api_key: str | None = None
) -> OllamaClient:
    return OllamaClient(base_url=base_url, api_key=api_key, transport=httpx.MockTransport(handler))


def test_native_origin_strips_one_trailing_v1() -> None:
    cases = {
        "http://192.168.1.79:11434/v1": "http://192.168.1.79:11434",
        "http://host:11434/v1/": "http://host:11434",
        "http://host:11434": "http://host:11434",
        "http://host:11434/": "http://host:11434",
        # A reverse proxy mounting Ollama under a prefix keeps the prefix.
        "http://proxy.example/ollama/v1": "http://proxy.example/ollama",
        # Exactly one ``/v1`` goes, so a doubled one is left alone.
        "http://host/v1/v1": "http://host/v1",
        "  http://host:11434/v1  ": "http://host:11434",
    }
    for base_url, expected in cases.items():
        assert native_origin(base_url) == expected, base_url


def test_validate_keep_alive_accepts_durations_and_sentinels() -> None:
    for accepted in ("5m", "1h", "30m", "45s", "120m", KEEP_ALIVE_FOREVER, KEEP_ALIVE_UNLOAD):
        assert validate_keep_alive(accepted) == accepted
    assert validate_keep_alive("  1h ") == "1h"
    assert DEFAULT_KEEP_ALIVE == "5m"
    for rejected in ("5", "5d", "", "forever", "1.5h", "-5m", "05m", "-2", "5 m"):
        with pytest.raises(ValueError, match="keep_alive must be a duration") as excinfo:
            validate_keep_alive(rejected)
        assert "-1 to keep the model loaded" in str(excinfo.value)


async def test_installed_models_parses_tags_sorted_by_name() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    models = await client.installed_models()
    await client.close()

    assert [str(request.url) for request in seen] == ["http://fake/api/tags"]
    assert [model.name for model in models] == [
        "bare:latest",
        "muse-glimmer:latest",
        "qwen3.8:latest",
    ]
    muse = models[1]
    assert muse.size_bytes == 18200000000
    assert muse.family == "llama"
    assert muse.parameter_size == "27.9B"
    assert muse.quantization == "Q4_K_M"
    # Nanoseconds are truncated to what datetime holds, and the instant is
    # kept in UTC whatever zone the host reported it in.
    assert muse.modified_at == datetime(2026, 8, 30, 19, 34, 56, 123456, tzinfo=UTC)
    assert models[2].modified_at == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    bare = models[0]
    assert bare.size_bytes == 0
    assert bare.family is None
    assert bare.parameter_size is None
    assert bare.quantization is None
    assert bare.modified_at is None


async def test_loaded_models_parses_ps_including_vram_and_context() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    loaded = await client.loaded_models()
    await client.close()

    assert [str(request.url) for request in seen] == ["http://fake/api/ps"]
    assert len(loaded) == 1
    model = loaded[0]
    assert model.name == "muse-glimmer:latest"
    assert model.size_bytes == 18200000000
    assert model.size_vram_bytes == 18200000000
    assert model.expires_at == datetime(2026, 9, 2, 10, 5, tzinfo=UTC)
    assert model.context_length == 8192


async def test_loaded_models_treats_zero_time_expiry_as_none() -> None:
    ps = {
        "models": [
            # Go's zero time is Ollama's "never"; a CPU-only host has no VRAM
            # figure and an older server sends no context_length at all.
            {"name": "cpu:latest", "size": 10, "expires_at": "0001-01-01T00:00:00Z"},
            {"name": "odd:latest", "size": 10, "size_vram": -1, "expires_at": "not a date"},
        ]
    }
    client = _client(_handler([], ps=ps))
    loaded = await client.loaded_models()
    await client.close()

    assert [model.name for model in loaded] == ["cpu:latest", "odd:latest"]
    assert loaded[0].expires_at is None
    assert loaded[0].size_vram_bytes == 0
    assert loaded[0].context_length is None
    assert loaded[1].expires_at is None
    assert loaded[1].size_vram_bytes == 0


async def test_show_model_reads_context_length_from_architecture_key_and_capabilities() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    details = await client.show_model("qwen3.8:latest")
    await client.close()

    assert str(seen[0].url) == "http://fake/api/show"
    assert json.loads(seen[0].content) == {"model": "qwen3.8:latest"}
    assert details.name == "qwen3.8:latest"
    assert details.family == "qwen3"
    assert details.parameter_size == "27.3B"
    assert details.quantization == "Q4_K_M"
    assert details.context_length == 40960
    assert details.capabilities == ("completion", "tools", "thinking")
    assert details.license == "Apache License 2.0"


async def test_show_model_tolerates_missing_fields_and_falls_back_on_any_context_key() -> None:
    sparse = {"model_info": {"llama.context_length": 8192, "llama.block_count": 32}}
    client = _client(_handler([], show=sparse))
    details = await client.show_model("bare:latest")
    assert details.context_length == 8192
    assert details.capabilities == ()
    assert details.license is None
    assert details.family is None
    await client.close()

    empty = {"details": {}, "model_info": {"general.architecture": "x"}, "license": "\n\n"}
    client = _client(_handler([], show=empty))
    details = await client.show_model("bare:latest")
    assert details.context_length is None
    assert details.license is None
    await client.close()

    long_license = {"license": "  " + "L" * 400 + "\nrest"}
    client = _client(_handler([], show=long_license))
    details = await client.show_model("bare:latest")
    assert details.license == "L" * 200
    await client.close()


async def test_load_model_posts_generate_without_prompt_and_long_timeout() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    result = await client.load_model("qwen3.8:latest")
    forever = await client.load_model("qwen3.8:latest", keep_alive=KEEP_ALIVE_FOREVER)
    await client.close()

    assert str(seen[0].url) == "http://fake/api/generate"
    assert json.loads(seen[0].content) == {
        "model": "qwen3.8:latest",
        "keep_alive": "5m",
        "stream": False,
    }
    # A cold multi-GB model can take minutes to read in; the read timeout
    # on this one call is the long one.
    assert seen[0].extensions["timeout"]["read"] == 600.0
    assert result.model == "qwen3.8:latest"
    assert result.done_reason == "load"
    assert result.latency_ms >= 0
    # The sentinel must be a JSON number: the string "-1" is what Ollama 400s on.
    assert json.loads(seen[1].content)["keep_alive"] == -1
    assert forever.done_reason == "load"


async def test_load_model_refuses_zero_keep_alive() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    with pytest.raises(ValueError, match="use unload_model to unload"):
        await client.load_model("qwen3.8:latest", keep_alive="0")
    with pytest.raises(ValueError, match="keep_alive must be a duration"):
        await client.load_model("qwen3.8:latest", keep_alive="5d")
    await client.close()
    assert seen == []


async def test_unload_model_sends_keep_alive_zero() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    result = await client.unload_model("muse-glimmer:latest")
    await client.close()

    assert json.loads(seen[0].content) == {
        "model": "muse-glimmer:latest",
        "keep_alive": 0,
        "stream": False,
    }
    assert seen[0].extensions["timeout"]["read"] == 30.0
    assert result.done_reason == "unload"


async def test_native_404_is_a_non_retryable_provider_error_with_ollamas_sentence() -> None:
    client = _client(_handler([]))
    with pytest.raises(ModelProviderError) as excinfo:
        await client.load_model("nope:latest")
    await client.close()

    assert str(excinfo.value) == (
        "ollama: HTTP 404: model 'nope:latest' not found, try pulling it first"
    )
    assert excinfo.value.status_code == 404
    assert excinfo.value.retryable is False


async def test_native_network_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.installed_models()
    await client.close()

    assert str(excinfo.value) == "ollama: network error: ConnectError"
    assert excinfo.value.retryable is True


async def test_native_5xx_is_retryable_and_non_object_bodies_are_provider_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json=[])

    client = _client(handler)
    with pytest.raises(ModelProviderError) as unavailable:
        await client.loaded_models()
    assert str(unavailable.value) == "ollama: HTTP 503: upstream down"
    assert unavailable.value.retryable is True
    with pytest.raises(ModelProviderError, match="/api/tags response was not an object"):
        await client.installed_models()
    await client.close()


async def test_load_timeout_says_the_model_is_still_loading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow disk")

    client = _client(handler)
    with pytest.raises(ModelProviderError) as excinfo:
        await client.load_model("qwen3.8:latest")
    await client.close()

    assert "qwen3.8:latest is still loading after 600 s" in str(excinfo.value)
    assert excinfo.value.retryable is True


async def test_api_key_rides_along_as_a_bearer_for_reverse_proxies() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen), api_key="proxy-token")
    await client.installed_models()
    await client.close()
    assert seen[0].headers["authorization"] == "Bearer proxy-token"


async def test_chat_still_goes_to_v1_and_close_closes_both_clients() -> None:
    seen: list[httpx.Request] = []
    client = _client(_handler(seen))
    response = await client.generate(
        ModelRequest(model="qwen3.8:latest", messages=(ModelMessage(role="user", content="hi"),))
    )
    assert response.text == "hi"
    assert str(seen[0].url) == "http://fake/v1/chat/completions"

    # A base URL without ``/v1`` still chats on it and manages on the origin.
    bare_seen: list[httpx.Request] = []
    bare = _client(_handler(bare_seen), base_url="http://host:11434")
    with pytest.raises(ModelProviderError):
        await bare.generate(
            ModelRequest(model="x", messages=(ModelMessage(role="user", content="hi"),))
        )
    await bare.installed_models()
    await bare.close()
    assert [str(request.url) for request in bare_seen] == [
        "http://host:11434/chat/completions",
        "http://host:11434/api/tags",
    ]

    await client.close()
    assert client._client.is_closed
    assert client._native.is_closed


async def test_as_ollama_client_unwraps_and_refuses_other_adapters() -> None:
    ollama = _client(_handler([]))
    assert isinstance(ollama, OllamaNativeClient)
    assert as_ollama_client(ollama) is ollama
    await ollama.close()

    class _Wrapper:
        provider_name = "ollama"

        def ollama_client(self) -> OllamaNativeClient:
            return ollama

    assert as_ollama_client(_Wrapper()) is ollama  # type: ignore[arg-type]

    generic = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(_handler([]))
    )
    with pytest.raises(OllamaUnsupported) as excinfo:
        as_ollama_client(generic)
    await generic.close()
    assert str(excinfo.value) == (
        "openai_compatible: local model management needs an Ollama provider"
    )
    assert excinfo.value.retryable is False
