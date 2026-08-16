"""Anthropic Messages API adapter.

``POST {base_url}/messages`` with ``x-api-key`` + ``anthropic-version``
headers. System messages are extracted into the top-level ``system`` field;
``max_tokens`` is mandatory on this API so a generous default applies when
the caller sets none.
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

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class AnthropicClient(ModelClient):
    provider_name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = ANTHROPIC_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            timeout=_DEFAULT_TIMEOUT,
            transport=transport,
        )

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens or _DEFAULT_MAX_TOKENS,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role != "system"
            ],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if stream:
            payload["stream"] = True
        payload.update(request.extra)
        return payload

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        try:
            response = await self._client.post(
                "/messages", json=self._payload(request, stream=False)
            )
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"anthropic: network error: {type(exc).__name__}", retryable=True
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise ModelProviderError(
                f"anthropic: HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        text = "".join(
            block.get("text", "")
            for block in body.get("content") or []
            if block.get("type") == "text"
        )
        usage = body.get("usage") or {}
        return ModelResponse(
            text=text,
            finish_reason=body.get("stop_reason") or "",
            model=body.get("model") or request.model,
            usage=ModelUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
            ),
            latency_ms=latency_ms,
            provider_request_id=body.get("id"),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", "/messages", json=payload) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode(errors="replace")
                    raise ModelProviderError(
                        f"anthropic: HTTP {response.status_code}: {text[:500]}",
                        status_code=response.status_code,
                        retryable=classify_retryable(response.status_code),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data:
                        continue
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"anthropic: network error during stream: {type(exc).__name__}", retryable=True
            ) from exc

    async def verify(self) -> str:
        """List models — cheap authenticated call, no token spend."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"anthropic: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"anthropic: verification failed: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        count = len(body.get("data") or []) if isinstance(body, dict) else 0
        return f"ok: {count} models visible"

    async def close(self) -> None:
        await self._client.aclose()
