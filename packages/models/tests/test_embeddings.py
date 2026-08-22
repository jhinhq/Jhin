"""Optional embedding capability: OpenAI-compatible adapter request shape,
batching, truncation and error handling (MockTransport), the fake provider's
deterministic pseudo-embeddings, the instrumented unwrap, unsupported
providers, and profile config parsing."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest

from jhin_models import (
    EmbeddingConfig,
    EmbeddingUnsupported,
    ModelProviderError,
    ModelUsage,
    as_embedding_client,
    build_model_client,
)
from jhin_models.embeddings import MAX_EMBEDDING_BATCH, MAX_EMBEDDING_INPUT_CHARS
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.testing import FakeOpenAIServer, deterministic_embedding
from jhin_models.testing.fake_openai import build_embeddings


def embeddings_response(inputs: list[str], *, dims: int = 3) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "embed-1",
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(i)] * dims}
            for i, _ in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": 5 * len(inputs), "total_tokens": 5 * len(inputs)},
    }


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


async def test_request_shape_dimensions_and_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=embeddings_response(seen["body"]["input"]),
            headers={"x-request-id": "req-1"},
        )

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.embed(["alpha", "beta"], model="embed-1", dimensions=3)
    finally:
        await client.close()

    assert seen["url"] == "http://fake/v1/embeddings"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {
        "model": "embed-1",
        "input": ["alpha", "beta"],
        "encoding_format": "float",
        "dimensions": 3,
    }
    assert result.vectors == ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    assert result.dimensions == 3
    assert result.model == "embed-1"
    assert result.usage == ModelUsage(input_tokens=10)
    assert result.provider_request_id == "req-1"


async def test_batches_and_truncates_inputs_preserving_order() -> None:
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batches.append(body["input"])
        # Out-of-order entries must be re-sorted by index.
        payload = embeddings_response(body["input"])
        payload["data"].reverse()
        return httpx.Response(200, json=payload)

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    texts = [f"text {i}" for i in range(MAX_EMBEDDING_BATCH + 5)]
    texts[0] = "x" * (MAX_EMBEDDING_INPUT_CHARS + 100)
    texts[1] = "   "
    try:
        result = await client.embed(texts, model="embed-1")
    finally:
        await client.close()

    assert [len(b) for b in batches] == [MAX_EMBEDDING_BATCH, 5]
    assert len(batches[0][0]) == MAX_EMBEDDING_INPUT_CHARS
    assert batches[0][1] == " "
    assert len(result.vectors) == len(texts)
    assert result.vectors[0] == (0.0, 0.0, 0.0)
    assert result.vectors[MAX_EMBEDDING_BATCH] == (0.0, 0.0, 0.0)
    assert result.vectors[MAX_EMBEDDING_BATCH + 4] == (4.0, 4.0, 4.0)
    assert result.usage.input_tokens == 5 * len(texts)


async def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request expected")

    client = OpenAICompatibleClient(
        base_url="http://fake/v1", transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.embed([], model="embed-1")
    finally:
        await client.close()
    assert result.vectors == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "usage": {}},
        {"data": [{"index": 0, "embedding": "nope"}], "usage": {}},
        {"data": [{"index": 0, "embedding": [1.0, "x"]}], "usage": {}},
        {"data": [{"index": 0, "embedding": [1.0, 2.0]}], "usage": {}},
    ],
)
async def test_malformed_vectors_are_provider_errors(payload: dict[str, Any]) -> None:
    client = OpenAICompatibleClient(
        base_url="http://fake/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(ModelProviderError) as excinfo:
            await client.embed(["input-text-canary"], model="embed-1", dimensions=3)
    finally:
        await client.close()
    assert "input-text-canary" not in str(excinfo.value)  # never echoes input text


async def test_http_errors_classify_retryable() -> None:
    client = OpenAICompatibleClient(
        base_url="http://fake/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"error": {}})),
    )
    try:
        with pytest.raises(ModelProviderError) as excinfo:
            await client.embed(["a"], model="embed-1")
    finally:
        await client.close()
    assert excinfo.value.status_code == 429
    assert excinfo.value.retryable is True


async def test_fake_provider_is_deterministic_and_semantic() -> None:
    with FakeOpenAIServer() as server:
        client = build_model_client("openai_compatible", base_url=server.base_url)
        try:
            embedder = as_embedding_client(client)
            first = await embedder.embed(["deploy on friday", "ship on friday"], model="fake-embed")
            second = await embedder.embed(["deploy on friday"], model="fake-embed", dimensions=16)
            with pytest.raises(ModelProviderError):
                await embedder.embed(["x"], model="always-fails")
        finally:
            await client.close()
    assert list(first.vectors[0]) == deterministic_embedding("deploy on friday")
    assert first.dimensions == 64
    assert second.dimensions == 16
    assert list(second.vectors[0]) == deterministic_embedding("deploy on friday", dimensions=16)
    related = cosine(list(first.vectors[0]), list(first.vectors[1]))
    unrelated = cosine(list(first.vectors[0]), deterministic_embedding("quarterly budget review"))
    assert related > 0.4 > unrelated
    assert first.usage.input_tokens > 0


def test_build_embeddings_handles_string_input() -> None:
    status, payload = build_embeddings({"model": "m", "input": "only one"})
    assert status == 200
    assert len(payload["data"]) == 1


async def test_anthropic_is_unsupported() -> None:
    client = build_model_client("anthropic", api_key="k")
    try:
        with pytest.raises(EmbeddingUnsupported):
            as_embedding_client(client)
    finally:
        await client.close()


def test_profile_config_parsing_and_bounds() -> None:
    assert EmbeddingConfig.from_profile_config(None).enabled is False
    assert EmbeddingConfig.from_profile_config({"embeddings": "no"}).enabled is False
    # Invalid blocks degrade to disabled instead of raising at read time.
    assert EmbeddingConfig.from_profile_config({"embeddings": {"enabled": True}}).enabled is False
    config = EmbeddingConfig.from_profile_config(
        {
            "embeddings": {
                "enabled": True,
                "model": "text-embedding-3-small",
                "dimensions": 256,
                "cost_micros_per_million": 20_000,
            }
        }
    )
    assert config.enabled and config.model == "text-embedding-3-small"
    assert config.dimensions == 256
    assert config.estimate_cost_micros(ModelUsage(input_tokens=1_000_000)) == 20_000
    with pytest.raises(ValueError):
        EmbeddingConfig.model_validate({"enabled": True, "model": "m", "dimensions": 0})
    with pytest.raises(ValueError):
        EmbeddingConfig.model_validate({"enabled": True, "model": "m", "dimensions": 5000})
    with pytest.raises(ValueError):
        EmbeddingConfig.model_validate({"enabled": True, "model": ""})
