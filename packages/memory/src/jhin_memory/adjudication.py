"""Gray-zone LLM adjudication for memory dedup (docs/architecture/memory.md).

The deterministic similarity rule (:mod:`jhin_memory.similarity`) classifies
every pair as ``duplicate`` / ``distinct`` / ``uncertain``. Uncertain pairs —
the same fact in different words that neither the lexical thresholds nor
embeddings can settle — may be adjudicated by the workspace **default**
chat profile: one compact request for all pairs, temperature 0, strict
parsing of ``{"verdicts": ["SAME"|"DIFFERENT", ...]}``.

Everything is **best-effort and fail-safe by contract**: a missing default
profile skips adjudication entirely, and a provider failure or unparseable
reply counts every pair as DIFFERENT — we never merge on doubt. Only the
pair texts and subjects are sent to the model (both are already
model-visible workspace content); nothing here logs memory content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from opentelemetry.trace import Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import ModelProfile, ModelProvider, Workspace
from jhin_models import ModelClient, ModelMessage, ModelRequest, build_model_client
from jhin_observability import JhinMetrics, get_logger, noop_metrics, noop_tracer
from jhin_secrets import SecretCrypto, SecretStore

logger = get_logger(__name__)

# Pair budgets: per apply call on the write path, per /memories/deduplicate
# call on the retroactive path.
MAX_APPLY_ADJUDICATED_PAIRS = 5
MAX_DEDUP_ADJUDICATED_PAIRS = 20

MAX_PAIR_TEXT_CHARS = 300
_MAX_OUTPUT_TOKENS = 400
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

ADJUDICATION_SYSTEM_PROMPT = (
    "You compare pairs of remembered statements from one workspace and decide "
    "whether the two statements in each pair record the SAME real-world fact. "
    "Answer SAME only when both statements convey the same fact even though the "
    "wording differs. Any changed value counts as DIFFERENT: a different day, "
    "date, number, amount, frequency, name, owner, or system makes the pair "
    "DIFFERENT. When unsure, answer DIFFERENT. "
    'Return ONLY a JSON object of the form {"verdicts": ["SAME"|"DIFFERENT", ...]} '
    "with exactly one verdict per pair, in order, and no prose and no markdown."
)


@dataclass(frozen=True)
class AdjudicationPair:
    """One uncertain pair: the two contents plus their subject keys."""

    content_a: str
    content_b: str
    subject_a: str | None = None
    subject_b: str | None = None


@runtime_checkable
class PairAdjudicator(Protocol):
    """What persistence and the dedup service need: SAME (True) / DIFFERENT
    (False) per pair, never raising."""

    async def adjudicate(
        self, pairs: Sequence[AdjudicationPair], *, workspace_id: UUID
    ) -> list[bool]: ...


class AdjudicationParseError(ValueError):
    """Model output was not a strictly valid verdict document."""


def build_adjudication_request(*, model: str, pairs: Sequence[AdjudicationPair]) -> ModelRequest:
    lines: list[str] = []
    for index, pair in enumerate(pairs, start=1):
        lines.append(f"Pair {index} (subjects: {pair.subject_a or '-'} | {pair.subject_b or '-'})")
        lines.append(f"A: {pair.content_a[:MAX_PAIR_TEXT_CHARS]}")
        lines.append(f"B: {pair.content_b[:MAX_PAIR_TEXT_CHARS]}")
    user = "Decide SAME or DIFFERENT for each pair.\n\n" + "\n".join(lines)
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(role="system", content=ADJUDICATION_SYSTEM_PROMPT),
            ModelMessage(role="user", content=user),
        ),
        temperature=0.0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )


def parse_adjudication(text: str, expected: int) -> list[bool]:
    """Deterministic strict parser (True = SAME). Raises
    :class:`AdjudicationParseError` on anything else."""
    raw = text.strip()
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1)
    if not raw:
        raise AdjudicationParseError("empty output")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdjudicationParseError(f"not JSON: {exc.msg}") from None
    if not isinstance(document, dict) or set(document.keys()) != {"verdicts"}:
        raise AdjudicationParseError("top level must be an object with exactly 'verdicts'")
    verdicts = document["verdicts"]
    if not isinstance(verdicts, list) or len(verdicts) != expected:
        raise AdjudicationParseError(f"'verdicts' must be a list of exactly {expected}")
    parsed: list[bool] = []
    for index, verdict in enumerate(verdicts):
        if not isinstance(verdict, str):
            raise AdjudicationParseError(f"verdict {index} is not a string")
        normalized = verdict.strip().upper()
        if normalized == "SAME":
            parsed.append(True)
        elif normalized == "DIFFERENT":
            parsed.append(False)
        else:
            raise AdjudicationParseError(f"verdict {index} is neither SAME nor DIFFERENT")
    return parsed


class MemoryAdjudicator:
    """One chat client bound to the workspace default profile.

    ``adjudicate`` never raises: provider failure or an unparseable reply
    logs the registered ``memory.adjudication_failed`` event and returns
    DIFFERENT for every pair. ``close`` releases the HTTP client.
    """

    def __init__(
        self,
        client: ModelClient,
        *,
        model: str,
        metrics: JhinMetrics | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._metrics = metrics if metrics is not None else noop_metrics()

    @property
    def model(self) -> str:
        return self._model

    async def adjudicate(
        self, pairs: Sequence[AdjudicationPair], *, workspace_id: UUID
    ) -> list[bool]:
        if not pairs:
            return []
        request = build_adjudication_request(model=self._model, pairs=pairs)
        try:
            response = await self._client.generate(request)
        except Exception as error:
            logger.warning(
                "memory.adjudication_failed",
                error_type=type(error).__name__,
                workspace_id=str(workspace_id),
                count=len(pairs),
            )
            return [False] * len(pairs)
        try:
            verdicts = parse_adjudication(response.text, len(pairs))
        except AdjudicationParseError:
            logger.warning(
                "memory.adjudication_failed",
                error_type="AdjudicationParseError",
                workspace_id=str(workspace_id),
                count=len(pairs),
            )
            return [False] * len(pairs)
        logger.info("memory.adjudicated", workspace_id=str(workspace_id), count=len(pairs))
        return verdicts

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as error:
            logger.warning("model.client_close_failed", error_type=type(error).__name__)


async def resolve_memory_adjudicator(
    session: AsyncSession,
    crypto: SecretCrypto | None,
    *,
    workspace_id: UUID,
    metrics: JhinMetrics | None = None,
    tracer: Tracer | None = None,
) -> MemoryAdjudicator | None:
    """Build the adjudicator for a workspace, or ``None`` when the workspace
    has no chat-capable **default** profile (no default profile, disabled or
    missing provider, unreadable credential). Never raises; the caller must
    ``close()``."""
    metrics = metrics if metrics is not None else noop_metrics()
    tracer = tracer if tracer is not None else noop_tracer()
    try:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None or workspace.default_model_profile_id is None:
            return None
        profile = await session.get(ModelProfile, workspace.default_model_profile_id)
        if profile is None or profile.workspace_id != workspace_id or not profile.model_name:
            return None
        provider = await session.scalar(
            select(ModelProvider).where(
                ModelProvider.id == profile.provider_id,
                ModelProvider.workspace_id == workspace_id,
                ModelProvider.enabled.is_(True),
            )
        )
        if provider is None:
            return None
        api_key: str | None = None
        if provider.secret_id is not None:
            if crypto is None:
                return None
            api_key = await SecretStore(session, crypto).reveal(workspace_id, provider.secret_id)
        client = build_model_client(
            provider.type,
            base_url=provider.base_url,
            api_key=api_key,
            metrics=metrics,
            tracer=tracer,
        )
        del api_key
    except Exception as error:
        logger.warning(
            "memory.adjudication_failed",
            error_type=type(error).__name__,
            workspace_id=str(workspace_id),
            count=0,
        )
        return None
    return MemoryAdjudicator(client, model=profile.model_name, metrics=metrics)
