"""Generic OpenAI-compatible chat-completions adapter (plan 15.1).

Speaks ``POST {base_url}/chat/completions`` with the standard OpenAI wire
format. OpenRouter and Ollama subclass this with different base URLs,
headers, and quirks; the first-party OpenAI adapter overrides the token-limit
field name.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jhin_models.base import (
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    classify_retryable,
)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class OpenAICompatibleClient(ModelClient):
    provider_name = "openai_compatible"
    # Legacy-but-universal field understood by vLLM/Ollama/OpenRouter/etc.
    max_tokens_field = "max_tokens"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=self._headers(api_key),
            timeout=_DEFAULT_TIMEOUT,
            transport=transport,
        )

    def _headers(self, api_key: str | None) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return headers

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload[self.max_tokens_field] = request.max_output_tokens
        payload.update(request.extra)
        return payload

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"{self.provider_name}: HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        return response

    def _parse_usage(self, usage: dict[str, Any]) -> ModelUsage:
        prompt_details = usage.get("prompt_tokens_details") or {}
        return ModelUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        response = await self._post("/chat/completions", self._payload(request, stream=False))
        latency_ms = int((time.monotonic() - started) * 1000)
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise ModelProviderError(f"{self.provider_name}: response contained no choices")
        message = choices[0].get("message") or {}
        return ModelResponse(
            text=message.get("content") or "",
            finish_reason=choices[0].get("finish_reason") or "",
            model=body.get("model") or request.model,
            usage=self._parse_usage(body.get("usage") or {}),
            latency_ms=latency_ms,
            provider_request_id=body.get("id"),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode(errors="replace")
                    raise ModelProviderError(
                        f"{self.provider_name}: HTTP {response.status_code}: {text[:500]}",
                        status_code=response.status_code,
                        retryable=classify_retryable(response.status_code),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data or data == "[DONE]":
                        continue
                    chunk = json.loads(data)
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error during stream: {type(exc).__name__}",
                retryable=True,
            ) from exc

    async def verify(self) -> str:
        """List models — the cheapest authenticated call on this API family."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"{self.provider_name}: verification failed: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        count = len(body.get("data") or []) if isinstance(body, dict) else 0
        return f"ok: {count} models visible"

    async def close(self) -> None:
        await self._client.aclose()
