"""First-party OpenAI adapter.

Identical wire format to the compatible base, but pinned to the official
endpoint and using ``max_completion_tokens`` (OpenAI deprecated ``max_tokens``
on chat completions).
"""

from __future__ import annotations

import httpx

from jhin_models.providers.openai_compatible import OpenAICompatibleClient

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIClient(OpenAICompatibleClient):
    provider_name = "openai"
    max_tokens_field = "max_completion_tokens"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, transport=transport)
