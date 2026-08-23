"""Factory dispatch, wrapping, and configuration validation."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest
from opentelemetry.trace import Tracer

from jhin_domain import ModelProviderType
from jhin_models import build_model_client
from jhin_models.base import ModelClient, ModelRequest, ModelResponse
from jhin_models.factory import ProviderConfigError
from jhin_models.providers.ollama import OLLAMA_BASE_URL
from jhin_models.providers.openai import OPENAI_BASE_URL
from jhin_models.providers.openrouter import OPENROUTER_BASE_URL
from jhin_observability import JhinMetrics, noop_metrics, noop_tracer


def _telemetry_module() -> Any:
    spec = importlib.util.find_spec("jhin_models.telemetry")
    assert spec is not None, "jhin_models.telemetry must provide the factory wrapper"
    return importlib.import_module("jhin_models.telemetry")


class _RawClient(ModelClient):
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.close_calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text=self.marker)

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            yield self.marker

        return chunks()

    async def verify(self) -> str:
        return self.marker

    async def close(self) -> None:
        self.close_calls += 1


async def test_factory_builds_each_adapter_behind_exactly_one_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = importlib.import_module("jhin_models.factory")
    module = _telemetry_module()
    wrapper_type = module.InstrumentedModelClient
    constructions: list[tuple[str, dict[str, object], _RawClient]] = []

    def constructor(name: str) -> Callable[..., _RawClient]:
        def build(**kwargs: object) -> _RawClient:
            raw = _RawClient(name)
            constructions.append((name, kwargs, raw))
            return raw

        return build

    for name in (
        "OpenAIClient",
        "AnthropicClient",
        "OpenRouterClient",
        "OllamaClient",
        "OpenAICompatibleClient",
    ):
        monkeypatch.setattr(factory, name, constructor(name))

    cases = [
        (ModelProviderType.OPENAI, {"api_key": "k"}, "OpenAIClient"),
        (ModelProviderType.ANTHROPIC, {"api_key": "k"}, "AnthropicClient"),
        (ModelProviderType.OPENROUTER, {"api_key": "k"}, "OpenRouterClient"),
        (ModelProviderType.OLLAMA, {}, "OllamaClient"),
        (
            ModelProviderType.OPENAI_COMPATIBLE,
            {"base_url": "http://fake:8080/v1"},
            "OpenAICompatibleClient",
        ),
    ]
    metrics = noop_metrics()
    tracer = noop_tracer()
    transport = cast(Any, object())
    for provider_type, kwargs, expected_name in cases:
        client = build_model_client(
            provider_type,
            **kwargs,
            transport=transport,
            metrics=metrics,
            tracer=tracer,
        )
        assert type(client) is wrapper_type
        raw = cast(Any, client)._wrapped
        assert type(raw) is _RawClient
        assert raw.marker == expected_name
        assert not isinstance(raw, wrapper_type)
        assert cast(Any, client)._metrics is metrics
        assert cast(Any, client)._tracer is tracer
        await client.close()
        assert raw.close_calls == 1

    assert [name for name, _kwargs, _raw in constructions] == [case[2] for case in cases]
    assert [kwargs for _name, kwargs, _raw in constructions] == [
        {
            "api_key": "k",
            "base_url": OPENAI_BASE_URL,
            "admin_api_key": None,
            "transport": transport,
        },
        {
            "api_key": "k",
            "base_url": "https://api.anthropic.com/v1",
            "transport": transport,
        },
        {"api_key": "k", "base_url": OPENROUTER_BASE_URL, "transport": transport},
        {"base_url": OLLAMA_BASE_URL, "api_key": None, "transport": transport},
        {"base_url": "http://fake:8080/v1", "api_key": None, "transport": transport},
    ]
    assert len({id(raw) for _name, _kwargs, raw in constructions}) == 5


async def test_factory_preserves_explicit_adapter_configuration_and_falsey_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = importlib.import_module("jhin_models.factory")
    observed: list[dict[str, object]] = []
    raw = _RawClient("configured")

    def configured(**kwargs: object) -> _RawClient:
        observed.append(kwargs)
        return raw

    monkeypatch.setattr(factory, "OpenAIClient", configured)

    class FalseyMetrics:
        def __bool__(self) -> bool:
            return False

    class FalseyTracer:
        def __bool__(self) -> bool:
            return False

    metrics = cast(JhinMetrics, FalseyMetrics())
    tracer = cast(Tracer, FalseyTracer())
    transport = cast(Any, object())
    client = build_model_client(
        ModelProviderType.OPENAI,
        api_key="explicit-key",
        base_url="https://explicit.example/v1",
        transport=transport,
        metrics=metrics,
        tracer=tracer,
    )

    assert observed == [
        {
            "api_key": "explicit-key",
            "base_url": "https://explicit.example/v1",
            "admin_api_key": None,
            "transport": transport,
        }
    ]
    assert cast(Any, client)._wrapped is raw
    assert cast(Any, client)._metrics is metrics
    assert cast(Any, client)._tracer is tracer
    await client.close()
    assert raw.close_calls == 1


async def test_factory_none_selects_only_explicit_noops_and_never_global_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = importlib.import_module("jhin_models.factory")
    module = _telemetry_module()
    raw = _RawClient("standalone")
    monkeypatch.setattr(factory, "OllamaClient", lambda **_kwargs: raw)

    bootstrap = importlib.import_module("jhin_observability.bootstrap")

    def forbidden_global() -> None:
        raise AssertionError("model factory must not read global observability")

    monkeypatch.setattr(bootstrap, "get_runtime", forbidden_global)
    client = build_model_client(ModelProviderType.OLLAMA)

    assert type(client) is module.InstrumentedModelClient
    assert cast(Any, client)._wrapped is raw
    assert cast(Any, client)._metrics is noop_metrics()
    assert cast(Any, client)._tracer is noop_tracer()
    assert (await client.generate(ModelRequest(model="local", messages=()))).text == "standalone"
    await client.close()
    assert raw.close_calls == 1


def test_factory_preserves_enum_conversion_as_provider_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = importlib.import_module("jhin_models.factory")
    calls: list[str] = []
    for name in (
        "OpenAIClient",
        "AnthropicClient",
        "OpenRouterClient",
        "OllamaClient",
        "OpenAICompatibleClient",
    ):
        monkeypatch.setattr(factory, name, lambda **_kwargs: calls.append("constructed"))

    with pytest.raises(ValueError):
        build_model_client("unknown-provider")
    assert calls == []


def test_factory_public_signature_accepts_exact_injected_handles() -> None:
    import inspect

    signature = inspect.signature(build_model_client)
    assert list(signature.parameters) == [
        "provider_type",
        "base_url",
        "api_key",
        "admin_api_key",
        "transport",
        "metrics",
        "tracer",
    ]
    assert signature.parameters["metrics"].default is None
    assert signature.parameters["tracer"].default is None
    assert "JhinMetrics" in str(signature.parameters["metrics"].annotation)
    assert "Tracer" in str(signature.parameters["tracer"].annotation)


def test_missing_api_key_is_rejected() -> None:
    for provider_type in (
        ModelProviderType.OPENAI,
        ModelProviderType.ANTHROPIC,
        ModelProviderType.OPENROUTER,
    ):
        with pytest.raises(ProviderConfigError):
            build_model_client(provider_type)


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ProviderConfigError):
        build_model_client(ModelProviderType.OPENAI_COMPATIBLE)
