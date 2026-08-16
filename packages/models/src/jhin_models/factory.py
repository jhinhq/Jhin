"""Construct the right adapter from provider configuration (plan 6.7)."""

from __future__ import annotations

import httpx

from jhin_domain import ModelProviderType
from jhin_models.base import ModelClient
from jhin_models.providers.anthropic import AnthropicClient
from jhin_models.providers.ollama import OLLAMA_BASE_URL, OllamaClient
from jhin_models.providers.openai import OPENAI_BASE_URL, OpenAIClient
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterClient


class ProviderConfigError(ValueError):
    """Provider configuration is incomplete (e.g. missing API key or URL)."""


def build_model_client(
    provider_type: ModelProviderType | str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelClient:
    kind = ModelProviderType(provider_type)
    match kind:
        case ModelProviderType.OPENAI:
            if not api_key:
                raise ProviderConfigError("OpenAI provider requires an API key secret")
            return OpenAIClient(
                api_key=api_key, base_url=base_url or OPENAI_BASE_URL, transport=transport
            )
        case ModelProviderType.ANTHROPIC:
            if not api_key:
                raise ProviderConfigError("Anthropic provider requires an API key secret")
            return AnthropicClient(
                api_key=api_key,
                base_url=base_url or "https://api.anthropic.com/v1",
                transport=transport,
            )
        case ModelProviderType.OPENROUTER:
            if not api_key:
                raise ProviderConfigError("OpenRouter provider requires an API key secret")
            return OpenRouterClient(
                api_key=api_key, base_url=base_url or OPENROUTER_BASE_URL, transport=transport
            )
        case ModelProviderType.OLLAMA:
            return OllamaClient(
                base_url=base_url or OLLAMA_BASE_URL, api_key=api_key, transport=transport
            )
        case ModelProviderType.OPENAI_COMPATIBLE:
            if not base_url:
                raise ProviderConfigError("OpenAI-compatible provider requires a base URL")
            return OpenAICompatibleClient(base_url=base_url, api_key=api_key, transport=transport)
