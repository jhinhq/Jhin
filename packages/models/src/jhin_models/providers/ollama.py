"""Ollama adapter: local OpenAI-compatible endpoint, no API key required."""

from __future__ import annotations

import httpx

from jhin_models.providers.openai_compatible import OpenAICompatibleClient

OLLAMA_BASE_URL = "http://localhost:11434/v1"


class OllamaClient(OpenAICompatibleClient):
    provider_name = "ollama"
    # Local models take no ``reasoning_effort``; an explicit profile setting
    # fails loudly rather than being silently dropped.
    reasoning_effort_supported = False

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, transport=transport)
