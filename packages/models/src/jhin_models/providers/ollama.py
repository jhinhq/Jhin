"""Ollama adapter: local OpenAI-compatible endpoint, no API key required.

Chat, verification and the model picker use the OpenAI-compatible routes
under the provider's ``base_url`` (``/chat/completions``, ``/models``).
Managing the models on the host — what is installed, what is resident in
memory, loading and unloading — has no OpenAI-compatible equivalent, so
those calls go to Ollama's own ``/api`` endpoints on the origin the base URL
points at (:func:`native_origin`).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict

from jhin_models.base import (
    ModelClient,
    ModelProviderError,
    classify_retryable,
    describe_error_body,
)
from jhin_models.providers.openai_compatible import OpenAICompatibleClient

OLLAMA_BASE_URL = "http://localhost:11434/v1"
# ``keep_alive`` is how long Ollama keeps a model resident after its last
# request. ``-1`` pins it until an explicit unload; ``0`` unloads it now.
DEFAULT_KEEP_ALIVE = "5m"
KEEP_ALIVE_FOREVER = "-1"
KEEP_ALIVE_UNLOAD = "0"
_KEEP_ALIVE_PATTERN = re.compile(r"^(-1|0|[1-9][0-9]*[smh])$")
# Listing and show are metadata reads; a load of an 18 GB model is not.
_NATIVE_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
_LOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
# Ollama's ``license`` field is the whole licence text; only its title is
# worth showing.
_LICENSE_LIMIT = 200
# Go's zero ``time.Time`` (``0001-01-01T00:00:00Z``) is Ollama's "never";
# anything before this is that sentinel, not a date.
_EARLIEST_REAL_YEAR = 2000


def native_origin(base_url: str) -> str:
    """The Ollama server root behind an OpenAI-compatible ``base_url``.

    The provider stores the ``/v1`` root the chat path uses; the native
    endpoints live one level up (``/api/tags`` and friends). Exactly one
    trailing ``/v1`` is dropped, so a reverse proxy that mounts Ollama under
    a prefix of its own keeps that prefix.
    """
    return base_url.strip().rstrip("/").removesuffix("/v1")


def validate_keep_alive(value: str) -> str:
    """``"5m"``/``"1h"``-style durations, ``"-1"`` (forever) or ``"0"`` (unload)."""
    cleaned = value.strip()
    if _KEEP_ALIVE_PATTERN.fullmatch(cleaned) is None:
        raise ValueError(
            "keep_alive must be a duration like 5m or 1h, -1 to keep the model loaded, "
            "or 0 to unload it"
        )
    return cleaned


def keep_alive_wire(value: str) -> int | str:
    """The JSON form Ollama accepts for a validated ``keep_alive``.

    A duration like ``"5m"`` is sent as a string and parsed by Go's
    ``time.ParseDuration``. That parser has no spelling for "forever", so the
    ``"-1"`` sentinel - and ``"0"`` with it - must travel as a JSON number,
    which Ollama reads as seconds; the string ``"-1"`` is a 400.
    """
    return int(value) if value in {KEEP_ALIVE_FOREVER, KEEP_ALIVE_UNLOAD} else value


def _parse_timestamp(value: object) -> datetime | None:
    """RFC 3339 as Go writes it: nanosecond fractions, and the zero time for
    "never". Anything unparseable is ``None`` rather than a guess."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.year < _EARLIEST_REAL_YEAR:
        return None
    # Normalised to UTC so the API serialises the same instant the same way
    # whatever zone the Ollama host runs in.
    return parsed.astimezone(UTC)


def _size(value: object) -> int:
    """A byte count from a JSON number; anything else counts as unknown (0)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return 0
    return int(value)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return int(value)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _details(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details")
    return details if isinstance(details, dict) else {}


def _model_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    """``/api/tags`` and ``/api/ps`` entries with a usable ``name``, by name."""
    rows = body.get("models")
    kept = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"].strip()
    ]
    return sorted(kept, key=lambda row: str(row["name"]))


class OllamaInstalledModel(BaseModel):
    """One model on the host's disk (``GET /api/tags``)."""

    model_config = ConfigDict(frozen=True)

    name: str
    size_bytes: int
    family: str | None
    parameter_size: str | None
    quantization: str | None
    modified_at: datetime | None


class OllamaLoadedModel(BaseModel):
    """One model resident in memory (``GET /api/ps``).

    ``size_vram_bytes`` is 0 on a CPU-only host; ``context_length`` is the
    context the running instance was started with, not the model's maximum.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    size_bytes: int
    size_vram_bytes: int
    expires_at: datetime | None
    context_length: int | None


class OllamaModelDetails(BaseModel):
    """What ``POST /api/show`` says about one installed model."""

    model_config = ConfigDict(frozen=True)

    name: str
    family: str | None
    parameter_size: str | None
    quantization: str | None
    # The architecture's maximum, not the ``num_ctx`` a run uses.
    context_length: int | None
    capabilities: tuple[str, ...]
    license: str | None


class OllamaLoadResult(BaseModel):
    """Outcome of a load or unload (``POST /api/generate`` without a prompt)."""

    model_config = ConfigDict(frozen=True)

    model: str
    # Ollama's own word: ``"load"``, ``"unload"``, or empty on older servers.
    done_reason: str
    latency_ms: int


class OllamaUnsupported(ModelProviderError):
    """The provider is not an Ollama server, so it has no models to manage."""

    def __init__(self, message: str = "local model management needs an Ollama provider") -> None:
        super().__init__(message, retryable=False)


@runtime_checkable
class OllamaNativeClient(Protocol):
    async def installed_models(self) -> list[OllamaInstalledModel]:
        """Every model on the host's disk, sorted by name."""

    async def loaded_models(self) -> list[OllamaLoadedModel]:
        """Every model currently resident in memory, sorted by name."""

    async def show_model(self, name: str) -> OllamaModelDetails:
        """Architecture, capabilities and licence of one installed model."""

    async def load_model(
        self, name: str, *, keep_alive: str = DEFAULT_KEEP_ALIVE
    ) -> OllamaLoadResult:
        """Bring a model into memory and keep it there for ``keep_alive``."""

    async def unload_model(self, name: str) -> OllamaLoadResult:
        """Drop a model from memory now."""


def as_ollama_client(client: ModelClient) -> OllamaNativeClient:
    if isinstance(client, OllamaNativeClient):
        return client
    unwrap = getattr(client, "ollama_client", None)
    if callable(unwrap):
        return cast(OllamaNativeClient, unwrap())
    provider = getattr(client, "provider_name", type(client).__name__)
    raise OllamaUnsupported(f"{provider}: local model management needs an Ollama provider")


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
        # Ollama itself takes no credential; a key, when set, is for a reverse
        # proxy in front of it and so goes on the native calls too.
        self._native = httpx.AsyncClient(
            base_url=native_origin(base_url),
            headers=self._headers(api_key),
            timeout=_NATIVE_TIMEOUT,
            transport=transport,
        )

    async def _native_get(self, path: str, *, timeouts: httpx.Timeout) -> dict[str, Any]:
        try:
            response = await self._native.get(path, timeout=timeouts)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        return self._native_body(path, response)

    async def _native_post(
        self, path: str, payload: dict[str, Any], *, timeouts: httpx.Timeout
    ) -> dict[str, Any]:
        try:
            response = await self._native.post(path, json=payload, timeout=timeouts)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        return self._native_body(path, response)

    def _native_body(self, path: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            # Ollama answers ``{"error": "model 'x' not found, try pulling it
            # first"}``; that sentence is the one worth showing.
            raise ModelProviderError(
                f"{self.provider_name}: HTTP {response.status_code}: "
                f"{describe_error_body(response.text)}",
                status_code=response.status_code,
                retryable=classify_retryable(response.status_code),
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ModelProviderError(f"{self.provider_name}: {path} response was not an object")
        return body

    async def installed_models(self) -> list[OllamaInstalledModel]:
        body = await self._native_get("/api/tags", timeouts=_NATIVE_TIMEOUT)
        models: list[OllamaInstalledModel] = []
        for row in _model_rows(body):
            details = _details(row)
            models.append(
                OllamaInstalledModel(
                    name=str(row["name"]),
                    size_bytes=_size(row.get("size")),
                    family=_text(details.get("family")),
                    parameter_size=_text(details.get("parameter_size")),
                    quantization=_text(details.get("quantization_level")),
                    modified_at=_parse_timestamp(row.get("modified_at")),
                )
            )
        return models

    async def loaded_models(self) -> list[OllamaLoadedModel]:
        body = await self._native_get("/api/ps", timeouts=_NATIVE_TIMEOUT)
        return [
            OllamaLoadedModel(
                name=str(row["name"]),
                size_bytes=_size(row.get("size")),
                size_vram_bytes=_size(row.get("size_vram")),
                expires_at=_parse_timestamp(row.get("expires_at")),
                context_length=_positive_int(row.get("context_length")),
            )
            for row in _model_rows(body)
        ]

    async def show_model(self, name: str) -> OllamaModelDetails:
        body = await self._native_post("/api/show", {"model": name}, timeouts=_NATIVE_TIMEOUT)
        details = _details(body)
        raw_capabilities = body.get("capabilities")
        capabilities = tuple(
            item
            for item in (raw_capabilities if isinstance(raw_capabilities, list) else [])
            if isinstance(item, str) and item
        )
        return OllamaModelDetails(
            name=name,
            family=_text(details.get("family")),
            parameter_size=_text(details.get("parameter_size")),
            quantization=_text(details.get("quantization_level")),
            context_length=_context_length(body.get("model_info")),
            capabilities=capabilities,
            license=_license_title(body.get("license")),
        )

    async def _preload(
        self, name: str, *, keep_alive: str, timeouts: httpx.Timeout
    ) -> OllamaLoadResult:
        # A generate call with no prompt is Ollama's documented way to load
        # (or, with ``keep_alive: 0``, unload) a model without running it.
        started = time.monotonic()
        body = await self._native_post(
            "/api/generate",
            {"model": name, "keep_alive": keep_alive_wire(keep_alive), "stream": False},
            timeouts=timeouts,
        )
        return OllamaLoadResult(
            model=str(body.get("model") or name),
            done_reason=str(body.get("done_reason") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def load_model(
        self, name: str, *, keep_alive: str = DEFAULT_KEEP_ALIVE
    ) -> OllamaLoadResult:
        keep_alive = validate_keep_alive(keep_alive)
        if keep_alive == KEEP_ALIVE_UNLOAD:
            raise ValueError("use unload_model to unload")
        try:
            return await self._preload(name, keep_alive=keep_alive, timeouts=_LOAD_TIMEOUT)
        except ModelProviderError as exc:
            if not isinstance(exc.__cause__, httpx.TimeoutException):
                raise
            # Reading a cold multi-GB model off disk can outlast even the long
            # timeout; say what is happening instead of "network error".
            raise ModelProviderError(
                f"{self.provider_name}: {name} is still loading after "
                f"{_LOAD_TIMEOUT.read:.0f} s; check the host has memory to spare, then try again",
                retryable=True,
            ) from exc

    async def unload_model(self, name: str) -> OllamaLoadResult:
        return await self._preload(name, keep_alive=KEEP_ALIVE_UNLOAD, timeouts=_NATIVE_TIMEOUT)

    async def close(self) -> None:
        await self._native.aclose()
        await super().close()


def _context_length(model_info: object) -> int | None:
    """The architecture's ``<arch>.context_length`` from ``/api/show``.

    The key is named after the architecture (``qwen3.context_length``); when
    ``general.architecture`` is missing, any ``*.context_length`` key will do.
    """
    if not isinstance(model_info, dict):
        return None
    architecture = model_info.get("general.architecture")
    if isinstance(architecture, str) and architecture:
        found = _positive_int(model_info.get(f"{architecture}.context_length"))
        if found is not None:
            return found
    for key, value in model_info.items():
        if isinstance(key, str) and key.endswith(".context_length"):
            found = _positive_int(value)
            if found is not None:
                return found
    return None


def _license_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for line in value.splitlines():
        title = line.strip()
        if title:
            return title[:_LICENSE_LIMIT]
    return None
