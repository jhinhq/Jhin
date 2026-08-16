"""Factory dispatch and configuration validation."""

import pytest

from jhin_domain import ModelProviderType
from jhin_models import build_model_client
from jhin_models.factory import ProviderConfigError
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.ollama import OllamaClient
from jhin_models.providers.openai import OpenAIClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.providers.openrouter import OpenRouterClient


async def test_factory_builds_each_adapter() -> None:
    cases = [
        (ModelProviderType.OPENAI, {"api_key": "k"}, OpenAIClient),
        (ModelProviderType.ANTHROPIC, {"api_key": "k"}, AnthropicClient),
        (ModelProviderType.OPENROUTER, {"api_key": "k"}, OpenRouterClient),
        (ModelProviderType.OLLAMA, {}, OllamaClient),
        (
            ModelProviderType.OPENAI_COMPATIBLE,
            {"base_url": "http://fake:8080/v1"},
            OpenAICompatibleClient,
        ),
    ]
    for provider_type, kwargs, expected in cases:
        client = build_model_client(provider_type, **kwargs)
        assert type(client) is expected
        await client.close()


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
