"""Optional text-embedding capability (curated memory: semantic retrieval).

Chat providers are never required to embed. A provider that can implements
:class:`EmbeddingClient`; callers use :func:`as_embedding_client` to discover
support and receive a clean :class:`EmbeddingUnsupported` otherwise, exactly
like the image-generation capability in :mod:`jhin_models.images`.

Bounds are enforced by the adapters: every input is truncated to
:data:`MAX_EMBEDDING_INPUT_CHARS` and requests are split into batches of at
most :data:`MAX_EMBEDDING_BATCH` inputs. Text is never logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jhin_models.base import ModelClient, ModelProviderError, ModelUsage

EMBEDDINGS_CONFIG_KEY = "embeddings"
MAX_EMBEDDING_BATCH = 64
MAX_EMBEDDING_INPUT_CHARS = 8_000
MAX_EMBEDDING_DIMENSIONS = 4_096


class EmbeddingUnsupported(ModelProviderError):
    """The provider (or profile) cannot produce embeddings."""

    def __init__(self, message: str = "embeddings are not supported") -> None:
        super().__init__(message, retryable=False)


class EmbeddingResult(BaseModel):
    """One vector per input, in input order; ``dimensions`` is their length."""

    model_config = ConfigDict(frozen=True)

    vectors: tuple[tuple[float, ...], ...]
    model: str = ""
    dimensions: int = 0
    usage: ModelUsage = ModelUsage()
    latency_ms: int = 0
    provider_request_id: str | None = None


class EmbeddingConfig(BaseModel):
    """Per-profile ``config_json.embeddings`` block.

    ``cost_micros_per_million`` is the operator-declared input-token price in
    micro-dollars per million tokens so embedding calls are accounted for
    like chat calls. ``dimensions`` is optional; when set it is sent to the
    provider (OpenAI ``dimensions``) and stored embeddings must match it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    enabled: bool = False
    model: str = Field(default="", max_length=200)
    dimensions: int | None = Field(default=None, ge=1, le=MAX_EMBEDDING_DIMENSIONS)
    cost_micros_per_million: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _model_required_when_enabled(self) -> EmbeddingConfig:
        if self.enabled and not self.model.strip():
            raise ValueError("embeddings.model is required when embeddings are enabled")
        return self

    @classmethod
    def from_profile_config(cls, config_json: dict[str, Any] | None) -> EmbeddingConfig:
        raw = (config_json or {}).get(EMBEDDINGS_CONFIG_KEY)
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValueError:
            return cls()

    def estimate_cost_micros(self, usage: ModelUsage) -> int:
        if not self.cost_micros_per_million:
            return 0
        return usage.input_tokens * self.cost_micros_per_million // 1_000_000


def bound_inputs(texts: Sequence[str]) -> list[str]:
    """Truncate every input to the adapter limit; blank inputs become a single
    space so providers that reject empty strings still return a vector."""
    bounded: list[str] = []
    for text in texts:
        clipped = text[:MAX_EMBEDDING_INPUT_CHARS]
        bounded.append(clipped if clipped.strip() else " ")
    return bounded


@runtime_checkable
class EmbeddingClient(Protocol):
    async def embed(
        self, texts: Sequence[str], *, model: str, dimensions: int | None = None
    ) -> EmbeddingResult:
        """Embed ``texts`` (any length; adapters batch and truncate)."""


def as_embedding_client(client: ModelClient) -> EmbeddingClient:
    if isinstance(client, EmbeddingClient):
        return client
    unwrap = getattr(client, "embedding_client", None)
    if callable(unwrap):
        return cast(EmbeddingClient, unwrap())
    provider = getattr(client, "provider_name", type(client).__name__)
    raise EmbeddingUnsupported(f"{provider}: embeddings are not supported")
