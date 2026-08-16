"""OpenRouter adapter: OpenAI-compatible with attribution headers."""

from __future__ import annotations

import httpx

from jhin_models.providers.openai_compatible import OpenAICompatibleClient

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterClient(OpenAICompatibleClient):
    provider_name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OPENROUTER_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, transport=transport)
        # Optional attribution headers OpenRouter uses for app rankings.
        self._client.headers.setdefault("HTTP-Referer", "https://jhin.ai")
        self._client.headers.setdefault("X-Title", "Jhin")
