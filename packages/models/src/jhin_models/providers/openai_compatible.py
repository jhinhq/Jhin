"""Generic OpenAI-compatible chat-completions adapter (plan 15.1).

Speaks ``POST {base_url}/chat/completions`` with the standard OpenAI wire
format. OpenRouter and Ollama subclass this with different base URLs,
headers, and quirks; the first-party OpenAI adapter overrides the token-limit
field name.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import AsyncIterator, Sequence
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
    quota_error,
    tool_name_from_wire,
    wire_tool_name,
)
from jhin_models.embeddings import MAX_EMBEDDING_BATCH, EmbeddingResult, bound_inputs
from jhin_models.images import DEFAULT_IMAGE_SIZE, GeneratedImage
from jhin_models.pricing import lookup_price, per_token_usd_to_micros_per_million
from jhin_models.tool_arguments import normalize_tool_arguments

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


def _always_inline_image_model(model: str) -> bool:
    """OpenAI's ``gpt-image-*`` / ``chatgpt-image-*`` models return base64
    inline unconditionally and 400 on an explicit ``response_format``."""
    name = model.strip().lower()
    return name.startswith(("gpt-image", "chatgpt-image"))


def _sniff_image_content_type(data: bytes) -> str:
    """Magic-number sniff for the three formats the normalizer accepts."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


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

    def _serialize_message(self, message: ModelMessage) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": wire_tool_name(call.name),
                        "arguments": call.arguments_json,
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            wire["tool_call_id"] = message.tool_call_id
        return wire

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [self._serialize_message(m) for m in request.messages],
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": wire_tool_name(tool.name),
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload[self.max_tokens_field] = request.max_output_tokens
        payload.update(request.extra)
        return payload

    def _http_error(self, status_code: int, body: str) -> ModelProviderError:
        """Classify a failed response: out-of-credit first, then generic."""
        quota = quota_error(self.provider_name, status_code, body)
        if quota is not None:
            return quota
        return ModelProviderError(
            f"{self.provider_name}: HTTP {status_code}: {describe_error_body(body)}",
            status_code=status_code,
            retryable=classify_retryable(status_code),
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise self._http_error(response.status_code, response.text)
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
        known_tools = [tool.name for tool in request.tools]
        tool_calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            name = function.get("name")
            if not raw.get("id") or not name:
                continue  # malformed entries are ignored, never guessed at
            tool_calls.append(
                ModelToolCall(
                    id=str(raw["id"]),
                    name=tool_name_from_wire(str(name), known_tools),
                    arguments_json=normalize_tool_arguments(function.get("arguments")),
                )
            )
        return ModelResponse(
            text=message.get("content") or "",
            finish_reason=choices[0].get("finish_reason") or "",
            model=body.get("model") or request.model,
            usage=self._parse_usage(body.get("usage") or {}),
            latency_ms=latency_ms,
            provider_request_id=body.get("id"),
            tool_calls=tuple(tool_calls),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        payload = self._payload(request, stream=True)
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode(errors="replace")
                    raise self._http_error(response.status_code, text)
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

    async def _model_rows(self) -> list[dict[str, Any]]:
        """Raw ``GET /models`` entries (dicts with a non-empty string ``id``)."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise ModelProviderError(
                f"{self.provider_name}: listing models failed: HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        return [
            row
            for row in (rows or [])
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
        ]

    async def list_models(self) -> list[str]:
        """Model identifiers from ``GET /models`` (sorted, deduplicated)."""
        return sorted({str(row["id"]) for row in await self._model_rows()})

    # Catalog used when the provider's model list carries no prices (the
    # first-party OpenAI adapter sets "openai"); None means no catalog.
    pricing_catalog: str | None = None

    def _listing(self, row: dict[str, Any]) -> ModelListing:
        """Pricing from the row's ``pricing`` block (OpenRouter shape: USD per
        token strings), else the static catalog, else unknown."""
        model_id = str(row["id"])
        pricing = row.get("pricing")
        context = row.get("context_length") or row.get("context_window")
        context_window = int(context) if isinstance(context, int | float) and context > 0 else None
        if isinstance(pricing, dict):
            prompt = per_token_usd_to_micros_per_million(pricing.get("prompt"))
            completion = per_token_usd_to_micros_per_million(pricing.get("completion"))
            if prompt is not None or completion is not None:
                return ModelListing(
                    id=model_id,
                    input_cost_micros_per_million=prompt,
                    output_cost_micros_per_million=completion,
                    context_window=context_window,
                    source="provider",
                )
        if self.pricing_catalog is not None:
            price = lookup_price(self.pricing_catalog, model_id)
            if price is not None:
                return ModelListing(
                    id=model_id,
                    input_cost_micros_per_million=price.input_cost_micros_per_million,
                    output_cost_micros_per_million=price.output_cost_micros_per_million,
                    context_window=context_window or price.context_window,
                    source="catalog",
                )
        return ModelListing(id=model_id, context_window=context_window)

    async def list_models_detailed(self) -> list[ModelListing]:
        """Models with pricing: live from the list when present, else catalog."""
        by_id: dict[str, ModelListing] = {}
        for row in await self._model_rows():
            listing = self._listing(row)
            by_id.setdefault(listing.id, listing)
        return [by_id[key] for key in sorted(by_id)]

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str,
        size: str = DEFAULT_IMAGE_SIZE,
        quality: str | None = None,
    ) -> GeneratedImage:
        """OpenAI Images API (``POST /images/generations``), base64 response.

        ``b64_json`` is requested explicitly so the adapter never follows a
        provider-supplied URL; the bytes come back inline and are handed to
        the caller for safe normalization. The ``gpt-image-*`` family always
        answers inline and rejects ``response_format`` outright, so the
        parameter is only sent to models that need it (DALL·E and most
        OpenAI-compatible gateways).
        """
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        if not _always_inline_image_model(model):
            payload["response_format"] = "b64_json"
        if quality:
            payload["quality"] = quality
        response = await self._post("/images/generations", payload)
        body = response.json()
        entries = body.get("data") or []
        encoded = entries[0].get("b64_json") if entries else None
        if not isinstance(encoded, str) or not encoded:
            raise ModelProviderError(
                f"{self.provider_name}: image response carried no inline image data"
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ModelProviderError(
                f"{self.provider_name}: image response was not valid base64"
            ) from exc
        content_type = (
            entries[0].get("content_type")
            or entries[0].get("mime_type")
            or _sniff_image_content_type(data)
        )
        return GeneratedImage(
            data=data,
            content_type=str(content_type),
            model=str(body.get("model") or model),
            provider_request_id=response.headers.get("x-request-id"),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        """OpenAI Embeddings API (``POST /embeddings``), float vectors.

        Inputs are truncated and sent in batches of ``MAX_EMBEDDING_BATCH``;
        vectors are reassembled in input order and validated for count and
        equal dimensions. The text itself never reaches logs or errors.
        """
        started = time.perf_counter()
        bounded = bound_inputs(texts)
        if not bounded:
            return EmbeddingResult(vectors=(), model=model, dimensions=dimensions or 0)
        vectors: list[tuple[float, ...]] = []
        usage = ModelUsage()
        responded_model = model
        request_id: str | None = None
        for start in range(0, len(bounded), MAX_EMBEDDING_BATCH):
            batch = bounded[start : start + MAX_EMBEDDING_BATCH]
            payload: dict[str, Any] = {"model": model, "input": batch, "encoding_format": "float"}
            if dimensions is not None:
                payload["dimensions"] = dimensions
            response = await self._post("/embeddings", payload)
            body = response.json()
            entries = body.get("data") if isinstance(body, dict) else None
            if not isinstance(entries, list) or len(entries) != len(batch):
                raise ModelProviderError(
                    f"{self.provider_name}: embedding response carried "
                    f"{len(entries) if isinstance(entries, list) else 0} vectors for "
                    f"{len(batch)} inputs"
                )
            ordered = sorted(
                (entry for entry in entries if isinstance(entry, dict)),
                key=lambda entry: int(entry.get("index") or 0),
            )
            for entry in ordered:
                raw = entry.get("embedding")
                if not isinstance(raw, list) or not raw:
                    raise ModelProviderError(
                        f"{self.provider_name}: embedding response carried a malformed vector"
                    )
                try:
                    vectors.append(tuple(float(v) for v in raw))
                except (TypeError, ValueError) as exc:
                    raise ModelProviderError(
                        f"{self.provider_name}: embedding response carried a malformed vector"
                    ) from exc
            batch_usage = body.get("usage") or {}
            usage = ModelUsage(
                input_tokens=usage.input_tokens + int(batch_usage.get("prompt_tokens") or 0),
                output_tokens=0,
                cached_tokens=0,
            )
            responded_model = str(body.get("model") or model)
            request_id = request_id or response.headers.get("x-request-id")
        width = len(vectors[0])
        if any(len(vector) != width for vector in vectors):
            raise ModelProviderError(
                f"{self.provider_name}: embedding response mixed vector dimensions"
            )
        if dimensions is not None and width != dimensions:
            raise ModelProviderError(
                f"{self.provider_name}: embedding response returned {width} dimensions, "
                f"expected {dimensions}"
            )
        return EmbeddingResult(
            vectors=tuple(vectors),
            model=responded_model,
            dimensions=width,
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_request_id=request_id,
        )

    async def close(self) -> None:
        await self._client.aclose()
