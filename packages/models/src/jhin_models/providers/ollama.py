"""Ollama adapter: local host, no API key required.

Chat speaks Ollama's own ``POST /api/chat`` on the origin the provider's
``base_url`` points at (:func:`native_origin`); verification and the model
picker still use the OpenAI-compatible ``/models`` route under ``base_url``.

Chat is native because the serving context window can only be set there.
Measured against Ollama 0.34.1, the identical ``{"options":{"num_ctx":8192}}``
loads the model at 8192 through ``/api/chat`` and at the server's own default
of 32768 through ``/v1/chat/completions``, which silently drops the option.
Jhin budgets a prompt against a window, so a profile that configures one gets
an instance loaded at that size instead of whatever the server booted with —
and a profile that configures none is left alone entirely
(:func:`requested_serving_window`), because ``num_ctx`` is an allocation and a
host running larger by its own configuration must not be shrunk to fit an
assumption.

Managing the models on the host — what is installed, what is resident in
memory, loading and unloading — has no OpenAI-compatible equivalent either
and lives on the same ``/api`` origin. Reading the window a resident instance
is *actually* being served with is one of those: :func:`measured_context_window`
asks ``/api/ps``, so a server that served less than Jhin asked for still
clamps the budget of the steps that follow.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict

from jhin_models.base import (
    MODEL_INCOMPATIBLE_REQUEST,
    ModelClient,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
    ModelUsage,
    classify_retryable,
    describe_error_body,
    tool_name_from_wire,
    wire_tool_name,
)
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.tool_arguments import normalize_tool_arguments
from jhin_models.tool_schemas import inline_local_schema_refs

OLLAMA_BASE_URL = "http://localhost:11434/v1"
# Chat, on Ollama's own API rather than its OpenAI-compatible shim.
NATIVE_CHAT_PATH = "/api/chat"
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


def requested_serving_window(*, provider_type: str, context_window: int | None) -> int | None:
    """The window every Jhin request against one model profile must ask for.

    Only a profile that *configures* a window asks for one. ``num_ctx`` is a
    literal allocation, so with no configured window Jhin says nothing and the
    host keeps whatever it was started with — an ``OLLAMA_CONTEXT_LENGTH`` of
    its own, or an instance another client loaded larger — exactly as before
    Jhin sent the option at all.

    One rule, in one place, because *every* path that reaches a given Ollama
    profile has to ask for the same window: Ollama treats a changed effective
    ``num_ctx`` as a reload of the model runner, so a path that asked for
    nothing would reload the instance at the host default underneath the path
    that asked for a window, and the budget would then measure the default and
    clamp to it.

    There is deliberately no ceiling here. A configured window is an operator's
    decision to allocate that much KV cache; capping it would serve a prompt
    quietly against a smaller window than the one they configured, which is the
    class of silence this whole path exists to end. A window too large for the
    host fails on the host, in the host's own words
    (:meth:`OllamaClient._chat` surfaces them).
    """
    if provider_type != OllamaClient.provider_name:
        return None
    if context_window is None or context_window <= 0:
        return None
    return context_window


def serving_window_options(context_window: int | None) -> dict[str, Any]:
    """The ``ModelRequest.extra`` that pins Ollama's serving window.

    ``num_ctx`` is how large an instance Ollama loads and therefore how much
    prompt it will accept before context shifting starts dropping the oldest
    turns. The caller that decides the window (the context budget) says so
    through this, so the number Jhin budgets against and the number it asks
    the server for are one number. Nothing to ask for means an empty ``extra``:
    no option, no reload.
    """
    if context_window is None or context_window <= 0:
        return {}
    return {"options": {"num_ctx": context_window}}


def _native_arguments(arguments_json: str) -> dict[str, Any]:
    """A tool call's arguments as ``/api/chat`` types them: an object.

    Jhin keeps the provider's arguments as JSON *text* (the OpenAI wire form)
    and hands that text to the gateway unparsed. Ollama's own wire types the
    field as a map, so echoing an earlier call back in the history means
    parsing it again here.

    Text that is not a JSON object cannot travel on this wire at all, and the
    whole request would be refused for it. An empty argument map keeps the
    turn -- and the ``invalid_input`` result the gateway already recorded next
    to it -- in the transcript, where the model can see what went wrong.
    """
    try:
        decoded = json.loads(arguments_json or "{}")
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _native_message(message: ModelMessage, tool_names: dict[str, str]) -> dict[str, Any]:
    """One Jhin turn as an Ollama chat message.

    ``tool_names`` maps a call id to the wire name it was requested under, so
    a result can name the tool it answers: measured against 0.34.1, that name
    is what makes the round trip work.
    """
    text = message.content
    images: list[str] = []
    for part in message.content_parts:
        if part.type == "image":
            # Native chat has no content blocks: an image travels as raw
            # base64 beside the turn's text, not as a data: URL inside it.
            images.append(part.data_base64)
        elif part.text:
            text = f"{text}\n{part.text}" if text else part.text
    wire: dict[str, Any] = {"role": message.role, "content": text}
    if images:
        wire["images"] = images
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "function": {
                    "name": wire_tool_name(call.name),
                    "arguments": _native_arguments(call.arguments_json),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        # Ollama pairs a result to its call by ``tool_name``; the id is sent
        # too and ignored by servers that do not read it yet.
        wire["tool_call_id"] = message.tool_call_id
        name = tool_names.get(message.tool_call_id)
        if name is not None:
            wire["tool_name"] = name
    return wire


def _native_messages(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    """The conversation as Ollama's ``messages`` array, in order."""
    tool_names: dict[str, str] = {}
    wire: list[dict[str, Any]] = []
    for message in messages:
        wire.append(_native_message(message, tool_names))
        for call in message.tool_calls:
            tool_names[call.id] = wire_tool_name(call.name)
    return wire


def _call_id(raw: object) -> str:
    """A tool call's id, preserved verbatim when the server sent one.

    Jhin pairs a result to its call by this id, so a call without one cannot
    be dispatched at all. Servers older than the release that added ids would
    otherwise lose every tool call; a synthesised id keeps the pairing local
    to this turn, which is all the pairing has to survive.
    """
    return str(raw) if isinstance(raw, str) and raw else f"call_{uuid4().hex[:8]}"


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


def _count(value: object) -> int:
    """A token count from a JSON number; anything else counts as unknown (0)."""
    return _size(value)


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


def _finish_reason(body: dict[str, Any]) -> str:
    """Ollama's ``done_reason`` in Jhin's finish-reason vocabulary.

    The two already agree where it matters: Ollama reports a generation that
    hit its output limit as ``"length"``, which is the exact word the worker
    refuses a truncated answer by, and a normal finish as ``"stop"``. So
    nothing is renamed — a reason Jhin holds no opinion about stays in
    Ollama's own words. A finished response carrying no reason at all (a
    server older than ``done_reason``) is a plain stop; an unfinished chunk
    has no reason yet.
    """
    reason = body.get("done_reason")
    if isinstance(reason, str) and reason:
        return reason
    return "stop" if body.get("done") else ""


def _native_usage(body: dict[str, Any]) -> ModelUsage:
    """Token counts from a finished ``/api/chat`` response.

    ``prompt_eval_count`` is the prompt Ollama evaluated and ``eval_count``
    what it generated. A reused KV prefix is reported separately as
    ``prompt_eval_cached_count`` and kept separate here too, exactly as the
    OpenAI-compatible route's ``cached_tokens``.
    """
    return ModelUsage(
        input_tokens=_count(body.get("prompt_eval_count")),
        output_tokens=_count(body.get("eval_count")),
        cached_tokens=_count(body.get("prompt_eval_cached_count")),
    )


def _native_tool_calls(
    message: dict[str, Any], known_tools: list[str]
) -> tuple[ModelToolCall, ...]:
    """Tool calls from a native message, in Jhin's shape."""
    calls: list[ModelToolCall] = []
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue  # malformed entries are ignored, never guessed at
        calls.append(
            ModelToolCall(
                id=_call_id(raw.get("id")),
                name=tool_name_from_wire(name, known_tools),
                # Ollama types arguments as an object where OpenAI types them
                # as a string; the normalizer serializes it back to the text
                # every consumer downstream already expects.
                arguments_json=normalize_tool_arguments(function.get("arguments")),
            )
        )
    return tuple(calls)


def _host_error(body: dict[str, Any]) -> str | None:
    """Ollama's own failure sentence from a 200 body, if it carries one.

    A request the host accepted but cannot serve — no memory for the window it
    was asked to load, a model that is not pulled — answers 200 with
    ``{"error": "..."}`` and no message. Read as an ordinary body that is an
    empty completion, and the one sentence saying why is thrown away. Both the
    buffered and the streamed path ask here, so both say the same thing.
    """
    if not body.get("error"):
        return None
    return describe_error_body(json.dumps(body))


def _wire_tool_call(raw: object) -> dict[str, Any] | None:
    """One native tool call in OpenAI delta shape, or ``None`` if unusable.

    Same rule as the buffered path (:func:`_native_tool_calls`): a call with no
    usable name is dropped rather than guessed at. It used to travel with an
    empty name instead, which the accumulator refused at the end of the stream
    — failing the whole step non-retryably where the buffered path had quietly
    carried on.
    """
    if not isinstance(raw, dict):
        return None
    function = raw.get("function")
    function = function if isinstance(function, dict) else {}
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    return {
        "id": _call_id(raw.get("id")),
        "function": {
            "name": name,
            "arguments": normalize_tool_arguments(function.get("arguments")),
        },
    }


def _stream_chunk(chunk: dict[str, Any], *, tool_index: int) -> tuple[dict[str, Any], int]:
    """One native NDJSON line as the OpenAI stream chunk the accumulator reads.

    Translating rather than re-implementing keeps one assembly path — output
    bounds, tool-identity checks and the "ended before completion" guard —
    for every provider. Tool calls need no delta accumulator: measured against
    0.34.1 each arrives whole in a single line, so each is given its own slot
    from ``tool_index`` instead of being merged with whatever shared the
    server's own numbering.
    """
    message = chunk.get("message")
    message = message if isinstance(message, dict) else {}
    delta: dict[str, Any] = {}
    content = message.get("content")
    if isinstance(content, str) and content:
        delta["content"] = content
    calls = [
        wire for wire in (_wire_tool_call(raw) for raw in message.get("tool_calls") or []) if wire
    ]
    if calls:
        # Only the calls that survived take a slot, so a dropped one never
        # leaves a gap the accumulator would read as a call that never arrived.
        delta["tool_calls"] = [
            {"index": tool_index + offset, **wire} for offset, wire in enumerate(calls)
        ]
    converted: dict[str, Any] = {
        "model": chunk.get("model"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": _finish_reason(chunk) or None}],
    }
    if chunk.get("done"):
        usage = _native_usage(chunk)
        converted["usage"] = {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "prompt_tokens_details": {"cached_tokens": usage.cached_tokens},
        }
    return converted, tool_index + len(calls)


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


@runtime_checkable
class OllamaContextProbe(Protocol):
    """Just enough of an Ollama client to ask what the host is serving.

    Deliberately narrower than :class:`OllamaNativeClient`: reading ``/api/ps``
    is a read every Ollama client can do, and a budget should not need the
    load/unload surface to find out how big the window is.
    """

    async def loaded_models(self) -> list[OllamaLoadedModel]:
        """Every model currently resident in memory, sorted by name."""


def _context_probe(client: object) -> OllamaContextProbe | None:
    if isinstance(client, OllamaContextProbe):
        return client
    unwrap = getattr(client, "ollama_client", None)
    if not callable(unwrap):
        return None
    try:
        candidate = unwrap()
    except ModelProviderError:
        # An instrumented wrapper raises :class:`OllamaUnsupported` here when
        # what it wraps is not an Ollama adapter. A profile can claim the
        # ollama provider type over any client; that mismatch must leave the
        # budget unmeasured, not fail the step.
        return None
    return candidate if isinstance(candidate, OllamaContextProbe) else None


def _same_model(requested: str, reported: str) -> bool:
    """``qwen3.8`` and ``qwen3.8:latest`` are the same model to Ollama."""
    left, right = requested.strip().lower(), reported.strip().lower()
    if not left or not right:
        return False
    return left == right or f"{left}:latest" == right or left == f"{right}:latest"


async def measured_context_window(client: object, *, provider_type: str, model: str) -> int | None:
    """The window the Ollama host reports it is *serving* for ``model``.

    ``GET /api/ps`` reports the ``context_length`` each resident instance was
    loaded with: the window the server will actually serve. Jhin asks for a
    window of its own (:func:`serving_window_options`) and ordinarily gets it,
    but the ask is not a guarantee — a host short of memory, one with a
    ceiling of its own, or an instance someone else loaded first can all serve
    less. This is how Jhin finds out, and it can only ever clamp downward.

    ``None`` — meaning "nothing measured", never "no limit" — when the
    provider is not Ollama, the client cannot reach the native API, the model
    is not resident, or the probe fails. A caller that gets ``None`` must fall
    back to its own conservative assumption, not to a larger window.
    """
    if provider_type != OllamaClient.provider_name:
        return None
    probe = _context_probe(client)
    if probe is None:
        return None
    try:
        loaded = await probe.loaded_models()
    except (ModelProviderError, httpx.HTTPError):
        # A budget that cannot measure falls back; it never fails the step for
        # want of a number it only wanted in order to be more careful.
        return None
    windows = [
        row.context_length
        for row in loaded
        if row.context_length is not None and _same_model(model, row.name)
    ]
    # Several instances of one model would each serve their own window; the
    # smallest is the only one every one of them can honour.
    return min(windows) if windows else None


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
        # Chat runs on the native client but keeps the generation timeouts the
        # base adapter configured; the native default is sized for metadata
        # reads, and a long answer would time out under it. The read budget is
        # the longer of that and a load, because asking for a window the
        # resident instance was not loaded with makes Ollama reload the runner
        # before the first token — the same multi-GB wait ``load_model``
        # already allows 600 s for, now part of an ordinary chat.
        self._chat_timeout = httpx.Timeout(
            connect=self._client.timeout.connect,
            # A changed num_ctx makes Ollama reload the runner, so a reload is
            # now part of the ordinary chat path and the read budget has to
            # cover it. It stops just short of the reasoning activity's own
            # ten-minute budget on purpose: whichever expires first is the one
            # that gets to explain, and the adapter can name the host's actual
            # complaint where a Temporal activity timeout cannot.
            read=max(self._client.timeout.read or 0.0, (_LOAD_TIMEOUT.read or 0.0) - 30.0),
            write=self._client.timeout.write,
            pool=self._client.timeout.pool,
        )
        # Ollama itself takes no credential; a key, when set, is for a reverse
        # proxy in front of it and so goes on the native calls too.
        self._native = httpx.AsyncClient(
            base_url=native_origin(base_url),
            headers=self._headers(api_key),
            timeout=_NATIVE_TIMEOUT,
            transport=transport,
        )

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        """The request as Ollama's native ``/api/chat`` body.

        Same request, different wire: tool calls carry object arguments, a
        tool result names the tool it answers, and every sampling parameter
        lives under ``options``. ``options.num_ctx`` is the one that matters —
        it is the window the instance is loaded with, and the caller who put
        it in ``extra`` (:func:`serving_window_options`) is the one budgeting
        the prompt against it. The OpenAI-compatible route drops that option,
        which is why chat left it.
        """
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _native_messages(request.messages),
            "stream": stream,
        }
        if request.tools:
            # Ollama's typed ToolProperty drops $ref before its model template
            # sees the tool. Keep the real nested object shape visible here.
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": wire_tool_name(tool.name),
                        "description": tool.description,
                        "parameters": inline_local_schema_refs(tool.parameters),
                    },
                }
                for tool in request.tools
            ]
        if request.web_search is not None and request.web_search.enabled:
            self._apply_web_search(payload, request.web_search)
        # Writes nothing for this provider; it is called to keep refusing an
        # explicit reasoning effort a local model cannot honor.
        self._apply_reasoning(payload, request)
        extra = dict(request.extra)
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        if "options" in extra:
            # The caller's own options land last, exactly as ``extra`` has
            # always outranked what the adapter filled in by itself — but only
            # as a map. Anything else cannot merge, and left in ``extra`` it
            # would be copied wholesale over this block below, taking the
            # temperature, the output limit and the num_ctx the prompt was
            # just budgeted against with it.
            caller_options = extra.pop("options")
            if not isinstance(caller_options, dict):
                raise ModelProviderError(
                    f"{self.provider_name}: options must be an object of Ollama options, "
                    f"not {type(caller_options).__name__}",
                    retryable=False,
                    error_code=MODEL_INCOMPATIBLE_REQUEST,
                )
            options.update(caller_options)
        if options:
            payload["options"] = options
        if "keep_alive" in extra:
            payload["keep_alive"] = self._keep_alive(extra.pop("keep_alive"))
        payload.update(extra)
        return payload

    def _keep_alive(self, value: object) -> int | str:
        """A caller's ``keep_alive`` in the form Ollama accepts.

        Same encoding as the management calls: durations stay strings, the
        ``-1``/``0`` sentinels must travel as JSON numbers or the server
        answers 400 (:func:`keep_alive_wire`).
        """
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return keep_alive_wire(validate_keep_alive(str(value)))
        except ValueError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: {exc}",
                retryable=False,
                error_code=MODEL_INCOMPATIBLE_REQUEST,
            ) from exc

    async def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One buffered ``/api/chat`` round trip, as a decoded object."""
        try:
            response = await self._native.post(
                NATIVE_CHAT_PATH, json=payload, timeout=self._chat_timeout
            )
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: network error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            # The chat route keeps the chat classification: a reverse proxy in
            # front of Ollama still answers 401, 429 and 5xx on its behalf.
            raise self._http_error(response.status_code, response.text)
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: chat response was not JSON", retryable=True
            ) from exc
        if not isinstance(body, dict):
            raise ModelProviderError(f"{self.provider_name}: chat response was not an object")
        error = _host_error(body)
        if error is not None:
            # 200 and an ``error`` key: the request was accepted and then could
            # not be served. Treated as a body it is an empty completion, which
            # discards the only sentence explaining why — the one that says a
            # window could not be allocated.
            raise ModelProviderError(f"{self.provider_name}: {error}", retryable=True)
        return body

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        body = await self._chat(self._payload(request, stream=False))
        latency_ms = int((time.monotonic() - started) * 1000)
        message = body.get("message")
        message = message if isinstance(message, dict) else {}
        # ``content`` only, exactly as on the OpenAI-compatible route: whatever
        # a thinking model returns beside the answer is ignored, and only the
        # answer reaches the conversation.
        content = message.get("content")
        return ModelResponse(
            text=content if isinstance(content, str) else "",
            finish_reason=_finish_reason(body),
            model=str(body.get("model") or request.model),
            usage=_native_usage(body),
            latency_ms=latency_ms,
            # Ollama issues no request id of its own; inventing one would put a
            # number in the run record that no log on the host can be found by.
            provider_request_id=None,
            tool_calls=_native_tool_calls(message, [tool.name for tool in request.tools]),
        )

    async def stream_events(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        from jhin_models.streaming import StreamAccumulator

        started = time.monotonic()
        accumulator = StreamAccumulator(request.model, [tool.name for tool in request.tools])
        payload = self._payload(request, stream=True)
        tool_index = 0
        try:
            async with self._native.stream(
                "POST", NATIVE_CHAT_PATH, json=payload, timeout=self._chat_timeout
            ) as response:
                if response.status_code >= 400:
                    raise self._http_error(
                        response.status_code, (await response.aread()).decode(errors="replace")
                    )
                # NDJSON, not SSE: one whole JSON object per line, the last one
                # carrying done, done_reason and the token counts.
                async for line in response.aiter_lines():
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        chunk = json.loads(text)
                    except ValueError as exc:
                        raise ModelProviderError(
                            f"{self.provider_name}: stream returned a line that was not JSON",
                            retryable=True,
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    error = _host_error(chunk)
                    if error is not None:
                        # Same sentence the buffered path raises; the generic
                        # "stream error" the accumulator would raise instead
                        # says nothing about which window would not load.
                        raise ModelProviderError(f"{self.provider_name}: {error}", retryable=True)
                    converted, tool_index = _stream_chunk(chunk, tool_index=tool_index)
                    for event in accumulator.openai(converted):
                        yield event
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"{self.provider_name}: stream transport failed", retryable=True
            ) from exc
        yield ModelStreamEvent(
            type="completed",
            response=accumulator.response(int((time.monotonic() - started) * 1000)),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Text deltas only, over the same native stream as everything else."""
        async for event in self.stream_events(request):
            if event.type == "text_delta" and event.text:
                yield event.text

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
