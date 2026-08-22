"""Embedding wiring for curated memory (docs/architecture/memory.md).

Everything here is **best-effort by contract**: an embedding failure leaves
the record without an embedding (retrieval then ranks it lexically), logs the
registered ``memory.embedding_failed`` event, and never raises into the
caller. Memory text is never logged.

Profile selection mirrors image generation: the agent's own model profile,
then the workspace default, then any enabled profile in the workspace whose
``config_json.embeddings.enabled`` is set and whose provider is enabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from opentelemetry.trace import Tracer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, MemoryRecord, ModelProfile, ModelProvider, Workspace
from jhin_domain import MemoryStatus
from jhin_memory.persistence import set_embedding
from jhin_models import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingResult,
    ModelClient,
    as_embedding_client,
    build_model_client,
)
from jhin_observability import JhinMetrics, MetricName, get_logger, noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto, SecretStore

logger = get_logger(__name__)

DEFAULT_BACKFILL_LIMIT = 100
MAX_BACKFILL_LIMIT = 500
_EMBEDDABLE_STATUSES = (MemoryStatus.ACTIVE.value, MemoryStatus.CONTESTED.value)
_TOKEN_METRIC = "model_tokens_total"
_COST_METRIC = "model_cost_estimate"


@dataclass(frozen=True)
class EmbeddingProfile:
    profile: ModelProfile
    provider: ModelProvider
    config: EmbeddingConfig


async def select_embedding_profile(
    session: AsyncSession, workspace_id: UUID, agent_id: UUID | None = None
) -> EmbeddingProfile | None:
    """The profile whose ``config_json.embeddings`` applies: agent profile →
    workspace default → any enabled profile (oldest first). ``None`` when no
    profile in the workspace enables embeddings."""
    candidates: list[UUID] = []
    if agent_id is not None:
        agent_profile_id = await session.scalar(
            select(Agent.model_profile_id).where(
                Agent.id == agent_id, Agent.workspace_id == workspace_id
            )
        )
        if agent_profile_id is not None:
            candidates.append(agent_profile_id)
    workspace = await session.get(Workspace, workspace_id)
    if workspace is not None and workspace.default_model_profile_id is not None:
        candidates.append(workspace.default_model_profile_id)
    ordered = list(
        await session.scalars(
            select(ModelProfile)
            .where(ModelProfile.workspace_id == workspace_id)
            .order_by(ModelProfile.created_at, ModelProfile.id)
        )
    )
    by_id = {profile.id: profile for profile in ordered}
    preferred = [by_id[pid] for pid in candidates if pid in by_id]
    for profile in preferred + [p for p in ordered if p.id not in candidates]:
        config = EmbeddingConfig.from_profile_config(profile.config_json)
        if not config.enabled or not config.model:
            continue
        provider = await session.scalar(
            select(ModelProvider).where(
                ModelProvider.id == profile.provider_id,
                ModelProvider.workspace_id == workspace_id,
                ModelProvider.enabled.is_(True),
            )
        )
        if provider is None:
            continue
        return EmbeddingProfile(profile=profile, provider=provider, config=config)
    return None


def _record_usage(
    metrics: JhinMetrics, *, provider_type: str, config: EmbeddingConfig, result: EmbeddingResult
) -> None:
    """Token and cost accounting through the same counters as chat calls."""
    try:
        tokens = result.usage.input_tokens
        if tokens > 0:
            metrics.counter(cast(MetricName, _TOKEN_METRIC)).add(
                tokens, provider_type=provider_type, direction="input"
            )
        cost_micros = config.estimate_cost_micros(result.usage)
        if cost_micros > 0:
            metrics.counter(cast(MetricName, _COST_METRIC)).add(
                cost_micros / 1_000_000, provider_type=provider_type
            )
    except Exception:  # diagnostics never affect the product path
        return


class MemoryEmbedder:
    """One embedding client bound to one profile configuration.

    ``embed_query`` / ``embed_records`` / ``embed_missing`` swallow provider
    errors (logging ``memory.embedding_failed``) and return what they could
    do; ``close`` releases the underlying HTTP client.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        config: EmbeddingConfig,
        provider_type: str,
        metrics: JhinMetrics | None = None,
    ) -> None:
        self._client = client
        self._embedder: EmbeddingClient = as_embedding_client(client)
        self._config = config
        self._provider_type = provider_type
        self._metrics = metrics if metrics is not None else noop_metrics()

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def dimensions(self) -> int | None:
        return self._config.dimensions

    async def _embed(self, texts: Sequence[str], *, workspace_id: UUID) -> EmbeddingResult | None:
        if not texts:
            return EmbeddingResult(vectors=(), model=self._config.model)
        try:
            result = await self._embedder.embed(
                texts, model=self._config.model, dimensions=self._config.dimensions
            )
        except Exception as error:
            logger.warning(
                "memory.embedding_failed",
                error_type=type(error).__name__,
                workspace_id=str(workspace_id),
                count=len(texts),
            )
            return None
        if len(result.vectors) != len(texts):
            logger.warning(
                "memory.embedding_failed",
                error_type="VectorCountMismatch",
                workspace_id=str(workspace_id),
                count=len(texts),
            )
            return None
        _record_usage(
            self._metrics, provider_type=self._provider_type, config=self._config, result=result
        )
        return result

    async def embed_query(self, text: str, *, workspace_id: UUID) -> list[float] | None:
        """The query vector for retrieval, or ``None`` on failure."""
        if not text.strip():
            return None
        result = await self._embed([text], workspace_id=workspace_id)
        if result is None:
            return None
        return list(result.vectors[0])

    async def embed_records(
        self, session: AsyncSession, records: Sequence[MemoryRecord], *, workspace_id: UUID
    ) -> int:
        """Attach embeddings to ``records`` that carry content; returns how
        many were embedded. Records keep no embedding on failure."""
        targets = [r for r in records if r.content and r.status in _EMBEDDABLE_STATUSES]
        if not targets:
            return 0
        result = await self._embed([r.content for r in targets], workspace_id=workspace_id)
        if result is None:
            return 0
        for record, vector in zip(targets, result.vectors, strict=True):
            await set_embedding(session, record, vector, model=self._config.model)
        logger.info("memory.embedded", workspace_id=str(workspace_id), count=len(targets))
        return len(targets)

    async def embed_missing(
        self, session: AsyncSession, *, workspace_id: UUID, limit: int = DEFAULT_BACKFILL_LIMIT
    ) -> tuple[int, int]:
        """Idempotent backfill: embed up to ``limit`` live records that have no
        embedding (or one from a different model). Returns
        ``(embedded, remaining)`` where ``remaining`` counts records still
        lacking an embedding for the current model after this batch."""
        limit = max(1, min(limit, MAX_BACKFILL_LIMIT))
        missing = [
            MemoryRecord.workspace_id == workspace_id,
            MemoryRecord.status.in_(_EMBEDDABLE_STATUSES),
            MemoryRecord.content != "",
            or_(
                MemoryRecord.embedding_json.is_(None),
                MemoryRecord.embedding_model != self._config.model,
            ),
        ]
        rows = list(
            await session.scalars(
                select(MemoryRecord)
                .where(*missing)
                .order_by(MemoryRecord.created_at, MemoryRecord.id)
                .limit(limit)
            )
        )
        embedded = await self.embed_records(session, rows, workspace_id=workspace_id)
        remaining = len(
            list(await session.scalars(select(MemoryRecord.id).where(*missing).limit(10_000)))
        )
        return embedded, remaining

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as error:
            logger.warning("model.client_close_failed", error_type=type(error).__name__)


async def resolve_memory_embedder(
    session: AsyncSession,
    crypto: SecretCrypto | None,
    *,
    workspace_id: UUID,
    agent_id: UUID | None = None,
    metrics: JhinMetrics | None = None,
    tracer: Tracer | None = None,
) -> MemoryEmbedder | None:
    """Build the embedder for a workspace/agent, or ``None`` when no profile
    enables embeddings, the credential cannot be read, or the provider does
    not support the capability. Never raises; the caller must ``close()``."""
    metrics = metrics if metrics is not None else noop_metrics()
    tracer = tracer if tracer is not None else noop_tracer()
    try:
        selected = await select_embedding_profile(session, workspace_id, agent_id)
        if selected is None:
            return None
        api_key: str | None = None
        if selected.provider.secret_id is not None:
            if crypto is None:
                return None
            api_key = await SecretStore(session, crypto).reveal(
                workspace_id, selected.provider.secret_id
            )
        client = build_model_client(
            selected.provider.type,
            base_url=selected.provider.base_url,
            api_key=api_key,
            metrics=metrics,
            tracer=tracer,
        )
        del api_key
    except Exception as error:
        logger.warning(
            "memory.embedding_failed",
            error_type=type(error).__name__,
            workspace_id=str(workspace_id),
            count=0,
        )
        return None
    try:
        return MemoryEmbedder(
            client,
            config=selected.config,
            provider_type=selected.provider.type,
            metrics=metrics,
        )
    except Exception as error:
        await client.close()
        logger.warning(
            "memory.embedding_failed",
            error_type=type(error).__name__,
            workspace_id=str(workspace_id),
            count=0,
        )
        return None
