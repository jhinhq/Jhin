"""OpenRouter adapter: OpenAI-compatible with attribution headers.

Balance comes from ``GET /credits`` (the client's base URL already ends in
``/api/v1``): ``total_credits`` purchased minus ``total_usage`` spent. Prices
come live from ``GET /models`` (``pricing.prompt``/``pricing.completion`` are
USD per token), handled by the base adapter's listing parser. A 402 means the
account is out of credit (``insufficient_funds``).
"""

from __future__ import annotations

from typing import Any

import httpx

from jhin_models.base import (
    AccountStatus,
    ModelProviderError,
    classify_retryable,
    describe_error_body,
)
from jhin_models.pricing import usd_to_micros
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.web_search import WebSearchConfig

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_CREDITS_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


def parse_credits(payload: dict[str, Any]) -> AccountStatus:
    """``{"data": {"total_credits": 50, "total_usage": 12.5}}`` → status."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ModelProviderError("openrouter: credits response carried no data")
    granted = usd_to_micros(data.get("total_credits"))
    used = usd_to_micros(data.get("total_usage"))
    if granted is None or used is None:
        raise ModelProviderError("openrouter: credits response was malformed")
    return AccountStatus(
        remaining_micros=granted - used,
        granted_micros=granted,
        spent_month_micros=None,
        source="openrouter",
        detail="Live from OpenRouter",
    )


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

    def _apply_web_search(self, payload: dict[str, Any], config: WebSearchConfig) -> None:
        """OpenRouter's ``web`` plugin (works with any underlying model).

        Citations come back as ``url_citation`` annotations on the message.
        """
        plugin: dict[str, Any] = {"id": "web"}
        if config.max_uses is not None:
            plugin["max_results"] = config.max_uses
        payload["plugins"] = [plugin]

    async def get_account_status(self) -> AccountStatus | None:
        try:
            response = await self._client.get("/credits", timeout=_CREDITS_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"openrouter: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"openrouter: credits HTTP {response.status_code}: "
                f"{describe_error_body(response.text)}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        if not isinstance(body, dict):
            raise ModelProviderError("openrouter: credits response was not an object")
        return parse_credits(body)
