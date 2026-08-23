"""Provider-neutral model interface and DTOs (plan 15.1, 15.4).

``ModelRequest``/``ModelResponse`` are the only shapes the agent runtime
sees. Adapters translate them to provider wire formats and back; nothing
provider-specific leaks out of this package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


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


class ModelProviderError(Exception):
    """Provider call failed.

    ``retryable`` classifies per plan 8.6: 408/429/5xx and network errors are
    retryable; auth and validation failures are not.
    """

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def classify_retryable(status_code: int) -> bool:
    return status_code in (408, 429) or status_code >= 500


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

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying HTTP client."""
