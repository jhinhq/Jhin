"""Provider-neutral model interface and DTOs (plan 15.1, 15.4).

``ModelRequest``/``ModelResponse`` are the only shapes the agent runtime
sees. Adapters translate them to provider wire formats and back; nothing
provider-specific leaks out of this package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jhin_models.reasoning import ReasoningConfig
from jhin_models.web_search import WebSearchConfig

Role = Literal["system", "user", "assistant", "tool"]


# Provider APIs (OpenAI, Anthropic) only accept ``^[a-zA-Z0-9_-]+$`` as a
# function/tool name, while Jhin's registry uses dotted names such as
# ``organization.delegate_task``. Adapters encode names for the wire and
# decode the model's tool calls back to registry names.
_WIRE_DOT = "__"


def wire_tool_name(name: str) -> str:
    """Registry tool name → provider-safe wire name (``a.b`` → ``a__b``)."""
    return name.replace(".", _WIRE_DOT)


def tool_name_from_wire(wire_name: str, known_names: Iterable[str] = ()) -> str:
    """Wire name → registry name.

    A name the request already offered is returned unchanged (covers fakes
    that echo dotted names); otherwise the encoding is reversed.
    """
    known = set(known_names)
    if wire_name in known:
        return wire_name
    decoded = wire_name.replace(_WIRE_DOT, ".")
    return decoded if decoded in known or not known else wire_name


class ModelToolCall(BaseModel):
    """One structured tool call the model requested (plan 12).

    ``arguments_json`` is the raw JSON string from the provider. It is parsed
    and schema-validated by the tool gateway, never trusted as-is; free text
    in ``ModelResponse.text`` is never interpreted as a tool call (plan 21.4).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments_json: str


class ModelMessage(BaseModel):
    """One conversation turn.

    ``tool_calls`` is set on assistant turns that requested tools;
    ``tool_call_id`` is set on ``tool`` turns carrying a tool's result back.
    """

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None


class ToolSchema(BaseModel):
    """Function signature advertised to the model (plan 7.2 layer 8).

    ``parameters`` is a JSON schema. Advertising a tool never authorizes it —
    the gateway decides every call (plan 12, 52).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    messages: tuple[ModelMessage, ...]
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: tuple[ToolSchema, ...] = ()
    # Model-native web search (docs/architecture/web.md): the provider runs
    # the search inside this call. Adapters that cannot honor an enabled
    # config raise ModelProviderError instead of silently ignoring it.
    web_search: WebSearchConfig | None = None
    # Reasoning-effort control for OpenAI-family models
    # (:mod:`jhin_models.reasoning`). None means "no profile opinion": the
    # adapter still pins ``reasoning_effort="none"`` when tools are present on
    # a reasoning-class model, because chat completions reject the two
    # together.
    reasoning: ReasoningConfig | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    finish_reason: str = ""
    model: str = ""
    usage: ModelUsage = ModelUsage()
    latency_ms: int = 0
    provider_request_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()


class ModelListing(BaseModel):
    """One model a provider exposes, with pricing when the source knows it.

    Costs are micro-dollars per million tokens (the profile's unit). ``source``
    names where the price came from: ``"provider"`` (live from the provider's
    model list), ``"catalog"`` (the static public price list in
    :mod:`jhin_models.pricing`), or ``None`` when nothing is known.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    input_cost_micros_per_million: int | None = None
    output_cost_micros_per_million: int | None = None
    context_window: int | None = None
    source: Literal["provider", "catalog"] | None = None


@dataclass(frozen=True)
class AccountStatus:
    """Provider-neutral account balance/spend snapshot.

    Every amount is in micro-dollars. A provider fills what its billing API
    exposes: OpenRouter reports granted credits and usage (so ``remaining`` is
    known); OpenAI's admin API reports month-to-date cost only (no balance
    API exists). ``source`` names the origin (``"openrouter"``,
    ``"openai_admin"``) and ``detail`` is a short human sentence for the UI.
    """

    remaining_micros: int | None = None
    spent_month_micros: int | None = None
    granted_micros: int | None = None
    period_start: date | None = None
    source: str = ""
    detail: str = ""


class AccountStatusUnsupported(Exception):
    """The adapter cannot report balance/spend with its current credentials."""


INSUFFICIENT_FUNDS = "insufficient_funds"
# The provider refused the request itself (not the account, not the load):
# the model cannot honor this combination of parameters. Today that is the
# reasoning-effort/function-tools conflict; the code is deliberately broader
# so future parameter conflicts can reuse it.
MODEL_INCOMPATIBLE_REQUEST = "model_incompatible_request"
_QUOTA_DASHBOARDS = {
    "openai": "https://platform.openai.com/settings/organization/billing",
    "openrouter": "https://openrouter.ai/settings/credits",
    "anthropic": "https://console.anthropic.com/settings/billing",
}
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "ollama": "Ollama",
    "openai_compatible": "OpenAI-compatible",
}


def insufficient_funds_message(provider_name: str) -> str:
    """The friendly out-of-credit sentence shown in run records and chat."""
    label = _PROVIDER_LABELS.get(provider_name, provider_name)
    dashboard = _QUOTA_DASHBOARDS.get(provider_name, "the provider's billing dashboard")
    return f"Your {label} account is out of credit. Add funds at {dashboard}, then retry."


class ModelProviderError(Exception):
    """Provider call failed.

    ``retryable`` classifies per plan 8.6: 408/429/5xx and network errors are
    retryable; auth and validation failures are not. ``error_code`` is a
    stable machine-readable class when one is known (``"insufficient_funds"``
    for out-of-credit responses) so run records and the chat can react.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.error_code = error_code


def classify_retryable(status_code: int) -> bool:
    return status_code in (408, 429) or status_code >= 500


def _error_code_from_body(text: str) -> str | None:
    import json

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        return str(code) if isinstance(code, str) else None
    code = payload.get("code")
    return str(code) if isinstance(code, str) else None


def quota_error(provider_name: str, status_code: int, body: str) -> ModelProviderError | None:
    """An ``insufficient_funds`` error when the response says the account is
    out of credit: OpenAI's 429 with ``code == "insufficient_quota"`` or
    OpenRouter's 402. Otherwise ``None`` (the caller raises its usual error).
    """
    out_of_credit = (
        status_code == 429 and _error_code_from_body(body) == "insufficient_quota"
    ) or (status_code == 402)
    if not out_of_credit:
        return None
    return ModelProviderError(
        insufficient_funds_message(provider_name),
        status_code=status_code,
        retryable=False,
        error_code=INSUFFICIENT_FUNDS,
    )


def _error_param_from_body(text: str) -> str | None:
    """``error.param`` from an OpenAI-shaped error body, when present."""
    import json

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    param = error.get("param")
    return str(param) if isinstance(param, str) else None


def reasoning_incompatible_message(provider_name: str, detail: str) -> str:
    """The friendly sentence for a rejected reasoning setting."""
    label = _PROVIDER_LABELS.get(provider_name, provider_name)
    return (
        f"{label} rejected this request because of the model's reasoning setting: "
        f"{detail} Fix it on the model profile (Advanced → Models): set "
        '`config_json.reasoning.effort` to "none" to keep tool calling, or pick a '
        "non-reasoning model."
    )


def reasoning_tool_conflict_message(provider_name: str, model_name: str, effort: str) -> str:
    """The friendly sentence for the conflict we can see *before* calling."""
    label = _PROVIDER_LABELS.get(provider_name, provider_name)
    return (
        f"{label}'s chat API cannot run '{model_name}' with tools while the model "
        f"profile pins reasoning effort to '{effort}'. Set "
        '`config_json.reasoning.effort` to "none" to keep tool calling, remove the '
        "setting to let Jhin do it automatically, or run an agent with no tools."
    )


def incompatible_request_error(
    provider_name: str, status_code: int, body: str
) -> ModelProviderError | None:
    """A ``model_incompatible_request`` error when the provider rejected the
    request's ``reasoning_effort`` (unsupported value, or the chat-completions
    "function tools with reasoning_effort" refusal). Otherwise ``None``.
    """
    if status_code != 400:
        return None
    detail = describe_error_body(body)
    if (
        "reasoning_effort" not in detail.lower()
        and _error_param_from_body(body) != "reasoning_effort"
    ):
        return None
    return ModelProviderError(
        reasoning_incompatible_message(provider_name, detail),
        status_code=status_code,
        retryable=False,
        error_code=MODEL_INCOMPATIBLE_REQUEST,
    )


def describe_error_body(text: str, *, limit: int = 500) -> str:
    """Human-readable detail from a provider error body.

    OpenAI-compatible and Anthropic APIs wrap failures as
    ``{"error": {"message": ...}}``; surface that message instead of the raw
    JSON so run records and chat transcripts read naturally. Non-JSON bodies
    are truncated as-is.
    """
    import json

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text[:limit]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"]).strip()[:limit]
        if isinstance(error, str):
            return error.strip()[:limit]
        if isinstance(payload.get("message"), str):
            return str(payload["message"]).strip()[:limit]
    return text[:limit]


class ModelClient(ABC):
    """One provider connection. Implementations must be fully async."""

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Single non-streaming completion."""

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        """Yield text deltas. Usage totals for streams arrive in Phase 4+."""

    @abstractmethod
    async def verify(self) -> str:
        """Cheap live credential/endpoint check. Returns a human summary."""

    async def list_models(self) -> list[str]:
        """Model identifiers the provider exposes, for pickers. Optional."""
        raise ModelProviderError(f"{type(self).__name__}: listing models is not supported")

    async def list_models_detailed(self) -> list[ModelListing]:
        """Models with pricing/context when known. Defaults to bare ids."""
        return [ModelListing(id=model_id) for model_id in await self.list_models()]

    async def get_account_status(self) -> AccountStatus | None:
        """Balance/spend from the provider's billing API when it has one.

        ``None`` means the provider has no such API (Anthropic, Ollama, generic
        endpoints); :class:`AccountStatusUnsupported` means it exists but this
        client lacks the credential for it (OpenAI without an admin key).
        """
        return None

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying HTTP client."""
