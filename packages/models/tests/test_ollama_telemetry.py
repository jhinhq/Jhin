"""The factory's wrapper exposes Ollama's native calls, with telemetry.

``installed_models`` and friends are not part of :class:`ModelClient`, so the
telemetry wrapper has to forward them explicitly (as it does
``fetch_model_costs``). Callers reach them through ``as_ollama_client``, which
means a missing forward would not fail loudly — it would raise
``OllamaUnsupported`` for every Ollama provider and read as "this host cannot
be managed".
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from jhin_models import (
    ModelProviderError,
    OllamaNativeClient,
    OllamaUnsupported,
    as_ollama_client,
    build_model_client,
)
from jhin_observability import noop_tracer
from jhin_observability.metrics import build_jhin_metrics

TAGS = {"models": [{"name": "qwen3.8:latest", "size": 17700000000, "details": {}}]}
PS: dict[str, Any] = {"models": []}
SHOW = {"model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/tags":
        return httpx.Response(200, json=TAGS)
    if path == "/api/ps":
        return httpx.Response(200, json=PS)
    if path == "/api/show":
        model = json.loads(request.content)["model"]
        if model == "nope:latest":
            return httpx.Response(404, json={"error": "model 'nope:latest' not found"})
        return httpx.Response(200, json=SHOW)
    if path == "/api/generate":
        body = json.loads(request.content)
        reason = "unload" if body["keep_alive"] == 0 else "load"
        return httpx.Response(200, json={"model": body["model"], "done_reason": reason})
    return httpx.Response(404, json={"error": f"unexpected {path}"})


def _metric_sum(reader: InMemoryMetricReader, name: str, **labels: str) -> float:
    data = reader.get_metrics_data()
    if data is None:
        return 0.0
    points: list[Any] = [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]
    return sum(float(point.value) for point in points if dict(point.attributes) == labels)


async def test_factory_exposes_an_ollama_client_for_ollama_providers() -> None:
    client = build_model_client(
        "ollama", base_url="http://fake/v1", transport=httpx.MockTransport(_handler)
    )
    unwrap = getattr(client, "ollama_client", None)
    assert unwrap is not None
    native = unwrap()
    assert isinstance(native, OllamaNativeClient)
    assert getattr(native, "provider_name", None) == "ollama"
    assert [model.name for model in await as_ollama_client(client).installed_models()] == [
        "qwen3.8:latest"
    ]
    await client.close()


async def test_instrumented_client_forwards_ollama_native_calls() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    metrics = build_jhin_metrics(provider.get_meter("ollama-test", "1"))
    client = build_model_client(
        "ollama",
        base_url="http://fake/v1",
        transport=httpx.MockTransport(_handler),
        metrics=metrics,
        tracer=noop_tracer(),
    )
    try:
        native = as_ollama_client(client)
        assert [model.name for model in await native.installed_models()] == ["qwen3.8:latest"]
        assert await native.loaded_models() == []
        assert (await native.show_model("qwen3.8:latest")).context_length == 40960
        assert (await native.load_model("qwen3.8:latest", keep_alive="1h")).done_reason == "load"
        assert (await native.unload_model("qwen3.8:latest")).done_reason == "unload"
        with pytest.raises(ModelProviderError):
            await native.show_model("nope:latest")
    finally:
        await client.close()
        provider.shutdown()

    # One attempt point per forwarded call, labelled with the real provider
    # type — including the load/unload pair whose operation name the
    # registry folds into "other".
    assert _metric_sum(reader, "model_requests_total", provider_type="ollama", outcome="ok") == 5
    assert (
        _metric_sum(reader, "model_requests_total", provider_type="ollama", outcome="failed") == 1
    )


async def test_instrumented_client_refuses_ollama_calls_for_other_adapters() -> None:
    client = build_model_client(
        "openai_compatible", base_url="http://fake/v1", transport=httpx.MockTransport(_handler)
    )
    with pytest.raises(OllamaUnsupported) as excinfo:
        cast(Any, client).ollama_client()
    with pytest.raises(OllamaUnsupported):
        as_ollama_client(client)
    await client.close()
    assert str(excinfo.value) == (
        "openai_compatible: local model management needs an Ollama provider"
    )
