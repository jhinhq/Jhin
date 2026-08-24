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
    ModelListing,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    classify_retryable,
    describe_error_body,
    tool_name_from_wire,
    wire_tool_name,
)
from jhin_models.pricing import lookup_price
from jhin_models.web_search import WebCitation, render_citations

ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
# Anthropic's server-side web search tool on /v1/messages (the broadly
# available basic variant; newer dated variants are model-gated).
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


def _web_citations(content: list[Any]) -> list[WebCitation]:
    """``web_search_result_location`` citations across the text blocks."""
    citations: list[WebCitation] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        for citation in block.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            if citation.get("type") != "web_search_result_location":
                continue
            url = citation.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = citation.get("title")
            citations.append(WebCitation(url=url, title=title if isinstance(title, str) else ""))
    return citations


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

    def _serialize_message(self, message: ModelMessage) -> dict[str, Any]:
        """Map the neutral message shape onto Anthropic content blocks.

        Assistant tool calls become ``tool_use`` blocks; tool results become
        user-role ``tool_result`` blocks (the Messages API convention).
        """
        if message.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": message.content,
                    }
                ],
            }
        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.arguments_json)
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": wire_tool_name(call.name),
                        "input": arguments,
                    }
                )
            return {"role": "assistant", "content": blocks}
        return {"role": message.role, "content": message.content}

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens or _DEFAULT_MAX_TOKENS,
            "messages": [
                self._serialize_message(m) for m in request.messages if m.role != "system"
            ],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        tools: list[dict[str, Any]] = [
            {
                "name": wire_tool_name(tool.name),
                "description": tool.description,
                "input_schema": tool.parameters or {"type": "object", "properties": {}},
            }
            for tool in request.tools
        ]
        if request.web_search is not None and request.web_search.enabled:
            # Server-side tool: the search runs inside this API call on
            # Anthropic's infrastructure — no Jhin tool effect involved.
            server_tool: dict[str, Any] = {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search"}
            if request.web_search.max_uses is not None:
                server_tool["max_uses"] = request.web_search.max_uses
            tools.append(server_tool)
        if tools:
            payload["tools"] = tools
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
            detail = describe_error_body(response.text)
            raise ModelProviderError(
                f"anthropic: HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        text = "".join(
            block.get("text", "")
            for block in body.get("content") or []
            if block.get("type") == "text"
        )
        text += render_citations(_web_citations(body.get("content") or []))
        known_tools = [tool.name for tool in request.tools]
        tool_calls = tuple(
            ModelToolCall(
                id=str(block["id"]),
                name=tool_name_from_wire(str(block["name"]), known_tools),
                arguments_json=json.dumps(block.get("input") or {}),
            )
            for block in body.get("content") or []
            if block.get("type") == "tool_use" and block.get("id") and block.get("name")
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
            tool_calls=tool_calls,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", "/messages", json=payload) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode(errors="replace")
                    detail = describe_error_body(text)
                    raise ModelProviderError(
                        f"anthropic: HTTP {response.status_code}: {detail}",
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

    async def list_models(self) -> list[str]:
        """Model identifiers from ``GET /models`` (sorted, deduplicated)."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"anthropic: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"anthropic: listing models failed: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        ids = {
            str(row["id"])
            for row in (rows or [])
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
        }
        return sorted(ids)

    async def list_models_detailed(self) -> list[ModelListing]:
        """Anthropic publishes no prices over the API: catalog lookup."""
        listings: list[ModelListing] = []
        for model_id in await self.list_models():
            price = lookup_price("anthropic", model_id)
            if price is None:
                listings.append(ModelListing(id=model_id))
                continue
            listings.append(
                ModelListing(
                    id=model_id,
                    input_cost_micros_per_million=price.input_cost_micros_per_million,
                    output_cost_micros_per_million=price.output_cost_micros_per_million,
                    context_window=price.context_window,
                    source="catalog",
                )
            )
        return listings

    async def close(self) -> None:
        await self._client.aclose()
