"""Local model management for Ollama providers (docs/architecture/models.md).

An Ollama provider is a host with models on disk, some of them resident in
memory. This module lists what the host has, loads and unloads, and stores
nothing on the provider row: ``last_error`` belongs to verification
(``service.py``), and a failed load is not a broken provider.

Reads never fail the request. Like ``list_provider_models`` they answer an
empty list plus a redacted ``detail`` when the host cannot be reached, so the
panel can say why instead of the page breaking. Mutations answer 200 with
``ok`` and a sentence, the ``verify_provider`` shape.

Loading is the one slow call. A cold 18 GB model can take longer than the
100 s a proxy in front of the API allows one request, so the load is awaited
for a short budget and then handed off: the response says the model is still
loading, the task finishes in the background, and the web's poll of
``/ollama/loaded`` flips the badge when the host reports it resident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from opentelemetry.trace import Tracer
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.models import service
from jhin_db.models import ModelProvider
from jhin_domain import ModelProviderType
from jhin_models import (
    KEEP_ALIVE_FOREVER,
    ModelClient,
    ModelProviderError,
    OllamaInstalledModel,
    OllamaLoadedModel,
    OllamaLoadResult,
    OllamaModelDetails,
    OllamaNativeClient,
    as_ollama_client,
)
from jhin_models.factory import ProviderConfigError
from jhin_observability import JhinMetrics, get_logger
from jhin_secrets import SecretCrypto
from jhin_secrets.redaction import redact_text

logger = get_logger(__name__)

# ``/api/tags`` and ``/api/ps`` are polled by the UI; a stuck host must not
# hold the panel hostage.
OLLAMA_LIST_TIMEOUT_SECONDS = 10.0
# Per-model ``/api/show`` inside the list merge.
OLLAMA_SHOW_TIMEOUT_SECONDS = 5.0
# Models beyond this many get no ``/api/show`` detail (context length and
# capabilities stay unknown) rather than fanning out without bound.
OLLAMA_SHOW_FANOUT_LIMIT = 24
# How long a load request is awaited before the response says "loading" and
# the load carries on in the background; see the module docstring.
OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS = 20.0
# Loads handed off past the budget. The set keeps a strong reference so the
# event loop does not garbage-collect a running task.
_BACKGROUND_LOADS: set[asyncio.Task[OllamaLoadResult]] = set()

_KEEPS_LOADED_HORIZON = timedelta(days=365)
_NOT_OLLAMA_DETAIL = (
    "This provider is not an Ollama endpoint; local model management only applies to "
    "providers of type ollama."
)
_TIMEOUT_DETAIL = "Ollama did not answer in time"

OllamaLoadStatus = Literal["loaded", "loading", "unloaded", "failed"]


@dataclass(frozen=True)
class OllamaModelRow:
    name: str
    size_bytes: int
    family: str | None
    parameter_size: str | None
    quantization: str | None
    modified_at: datetime | None
    context_length: int | None
    capabilities: list[str]
    loaded: bool
    size_vram_bytes: int | None
    expires_at: datetime | None
    keeps_loaded: bool


@dataclass(frozen=True)
class OllamaModelsSnapshot:
    models: list[OllamaModelRow]
    detail: str | None
    fetched_at: datetime


@dataclass(frozen=True)
class OllamaLoadedRow:
    name: str
    size_bytes: int
    size_vram_bytes: int
    expires_at: datetime | None
    keeps_loaded: bool
    context_length: int | None


@dataclass(frozen=True)
class OllamaLoadedSnapshot:
    models: list[OllamaLoadedRow]
    detail: str | None
    fetched_at: datetime


@dataclass(frozen=True)
class OllamaLoadOutcome:
    ok: bool
    status: OllamaLoadStatus
    model: str
    keep_alive: str | None
    detail: str


def keeps_loaded(expires_at: datetime | None, now: datetime) -> bool:
    """Whether the host will keep the model resident indefinitely.

    ``keep_alive: -1`` shows in ``/api/ps`` as an ``expires_at`` centuries
    out (or as Go's zero time, which the adapter reads as ``None``); neither
    is a date anyone should be shown.
    """
    return expires_at is None or expires_at - now > _KEEPS_LOADED_HORIZON


def _require_ollama(provider: ModelProvider) -> None:
    if provider.type != ModelProviderType.OLLAMA.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_OLLAMA_DETAIL)


async def _native(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider: ModelProvider,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> tuple[ModelClient, OllamaNativeClient]:
    """The adapter and its native view; the caller closes the adapter."""
    client = await service.provider_client(db, crypto, ctx.workspace_id, provider, metrics, tracer)
    try:
        return client, as_ollama_client(client)
    except ModelProviderError:
        await client.close()
        raise


def _describe(exc: BaseException) -> str:
    """The sentence a failed call turns into, with any credential redacted."""
    if isinstance(exc, TimeoutError):
        return _TIMEOUT_DETAIL
    if isinstance(exc, ProviderConfigError):
        return str(exc)
    redacted: str = redact_text(str(exc))
    return redacted


def _loaded_row(model: OllamaLoadedModel, now: datetime) -> OllamaLoadedRow:
    return OllamaLoadedRow(
        name=model.name,
        size_bytes=model.size_bytes,
        size_vram_bytes=model.size_vram_bytes,
        expires_at=model.expires_at,
        keeps_loaded=keeps_loaded(model.expires_at, now),
        context_length=model.context_length,
    )


def _merge_row(
    installed: OllamaInstalledModel,
    loaded: OllamaLoadedModel | None,
    details: OllamaModelDetails | None,
    now: datetime,
) -> OllamaModelRow:
    return OllamaModelRow(
        name=installed.name,
        size_bytes=installed.size_bytes,
        family=installed.family,
        parameter_size=installed.parameter_size,
        quantization=installed.quantization,
        modified_at=installed.modified_at,
        context_length=details.context_length if details is not None else None,
        capabilities=list(details.capabilities) if details is not None else [],
        loaded=loaded is not None,
        size_vram_bytes=loaded.size_vram_bytes if loaded is not None else None,
        expires_at=loaded.expires_at if loaded is not None else None,
        keeps_loaded=keeps_loaded(loaded.expires_at, now) if loaded is not None else False,
    )


async def _show_many(native: OllamaNativeClient, names: list[str]) -> dict[str, OllamaModelDetails]:
    """``/api/show`` for each name; one failing show leaves that row without
    detail and never fails the list."""
    results = await asyncio.gather(
        *(asyncio.wait_for(native.show_model(name), OLLAMA_SHOW_TIMEOUT_SECONDS) for name in names),
        return_exceptions=True,
    )
    return {
        name: result
        for name, result in zip(names, results, strict=True)
        if isinstance(result, OllamaModelDetails)
    }


async def list_ollama_models(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> OllamaModelsSnapshot:
    """Installed models merged with what is loaded and what ``/api/show`` knows.

    An unreachable host yields an empty list with the reason. A failing
    ``/api/ps`` degrades to "nothing loaded" with its reason in ``detail``,
    because the installed list is still worth showing.
    """
    provider = await service.get_provider(db, ctx.workspace_id, provider_id)
    _require_ollama(provider)
    now = datetime.now(UTC)
    try:
        client, native = await _native(db, crypto, ctx, provider, metrics, tracer)
    except (ProviderConfigError, ModelProviderError) as exc:
        return OllamaModelsSnapshot(models=[], detail=_describe(exc), fetched_at=now)
    try:
        try:
            installed = await asyncio.wait_for(
                native.installed_models(), OLLAMA_LIST_TIMEOUT_SECONDS
            )
        except (ModelProviderError, TimeoutError) as exc:
            return OllamaModelsSnapshot(models=[], detail=_describe(exc), fetched_at=now)
        detail: str | None = None
        loaded: list[OllamaLoadedModel] = []
        try:
            loaded = await asyncio.wait_for(native.loaded_models(), OLLAMA_LIST_TIMEOUT_SECONDS)
        except (ModelProviderError, TimeoutError) as exc:
            detail = _describe(exc)
        loaded_by_name = {model.name: model for model in loaded}
        details = await _show_many(
            native, [model.name for model in installed[:OLLAMA_SHOW_FANOUT_LIMIT]]
        )
        rows = [
            _merge_row(model, loaded_by_name.get(model.name), details.get(model.name), now)
            for model in installed
        ]
        rows.sort(key=lambda row: row.name)
        return OllamaModelsSnapshot(models=rows, detail=detail, fetched_at=now)
    finally:
        await client.close()


async def list_loaded_models(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
) -> OllamaLoadedSnapshot:
    """What the host holds in memory right now (``/api/ps``)."""
    provider = await service.get_provider(db, ctx.workspace_id, provider_id)
    _require_ollama(provider)
    now = datetime.now(UTC)
    try:
        client, native = await _native(db, crypto, ctx, provider, metrics, tracer)
    except (ProviderConfigError, ModelProviderError) as exc:
        return OllamaLoadedSnapshot(models=[], detail=_describe(exc), fetched_at=now)
    try:
        loaded = await asyncio.wait_for(native.loaded_models(), OLLAMA_LIST_TIMEOUT_SECONDS)
    except (ModelProviderError, TimeoutError) as exc:
        return OllamaLoadedSnapshot(models=[], detail=_describe(exc), fetched_at=now)
    finally:
        await client.close()
    rows = sorted((_loaded_row(model, now) for model in loaded), key=lambda row: row.name)
    return OllamaLoadedSnapshot(models=rows, detail=None, fetched_at=now)


async def show_ollama_model(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    name: str,
) -> tuple[OllamaModelDetails | None, str | None]:
    """``/api/show`` for one model, or an explanation."""
    provider = await service.get_provider(db, ctx.workspace_id, provider_id)
    _require_ollama(provider)
    try:
        client, native = await _native(db, crypto, ctx, provider, metrics, tracer)
    except (ProviderConfigError, ModelProviderError) as exc:
        return None, _describe(exc)
    try:
        return await asyncio.wait_for(native.show_model(name), OLLAMA_LIST_TIMEOUT_SECONDS), None
    except (ModelProviderError, TimeoutError) as exc:
        return None, _describe(exc)
    finally:
        await client.close()


def _loaded_detail(model: str, keep_alive: str) -> str:
    if keep_alive == KEEP_ALIVE_FOREVER:
        return f"{model} is loaded and stays in memory until you unload it."
    return f"{model} is loaded and stays in memory for {keep_alive} after its last request."


async def _load_and_close(
    client: ModelClient, native: OllamaNativeClient, model: str, keep_alive: str
) -> OllamaLoadResult:
    """The load itself. Owns the adapter: whether awaited by the request or
    finishing after it answered, the HTTP client is closed here and only here."""
    try:
        return await native.load_model(model, keep_alive=keep_alive)
    finally:
        await client.close()


def _log_background_load(task: asyncio.Task[OllamaLoadResult]) -> None:
    """The request already answered ``loading``; the only place the outcome
    can still be seen is the log (and the ``/loaded`` poll, when it worked)."""
    _BACKGROUND_LOADS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as error:
        logger.warning(
            "ollama.background_load_failed",
            error_type=type(error).__name__,
            error=_describe(error),
        )


async def load_ollama_model(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    model: str,
    keep_alive: str,
    request_id: UUID,
    ip_hash: str,
) -> OllamaLoadOutcome:
    """Load ``model`` into memory, answering within the response budget.

    The adapter's own read timeout is ten minutes; the request waits at most
    ``OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS`` for it. Past that, the shielded
    task keeps running (it owns and closes the client) and the outcome is
    ``loading`` — a success the ``/ollama/loaded`` poll confirms.
    """
    provider = await service.get_provider(db, ctx.workspace_id, provider_id)
    _require_ollama(provider)
    outcome: OllamaLoadOutcome
    try:
        client, native = await _native(db, crypto, ctx, provider, metrics, tracer)
    except (ProviderConfigError, ModelProviderError) as exc:
        outcome = OllamaLoadOutcome(
            ok=False, status="failed", model=model, keep_alive=keep_alive, detail=_describe(exc)
        )
    else:
        task = asyncio.create_task(_load_and_close(client, native, model, keep_alive))
        _BACKGROUND_LOADS.add(task)
        task.add_done_callback(_BACKGROUND_LOADS.discard)
        try:
            await asyncio.wait_for(asyncio.shield(task), OLLAMA_LOAD_RESPONSE_BUDGET_SECONDS)
        except TimeoutError:
            task.add_done_callback(_log_background_load)
            outcome = OllamaLoadOutcome(
                ok=True,
                status="loading",
                model=model,
                keep_alive=keep_alive,
                detail=f"Ollama is still loading {model}. It will show as loaded when it finishes.",
            )
        except (ModelProviderError, ValueError) as exc:
            outcome = OllamaLoadOutcome(
                ok=False,
                status="failed",
                model=model,
                keep_alive=keep_alive,
                detail=_describe(exc),
            )
        else:
            outcome = OllamaLoadOutcome(
                ok=True,
                status="loaded",
                model=model,
                keep_alive=keep_alive,
                detail=_loaded_detail(model, keep_alive),
            )
    audit.record(
        db,
        action="provider.ollama_model_loaded"
        if outcome.ok
        else "provider.ollama_model_load_failed",
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "display_name": provider.display_name,
            "model": model,
            "keep_alive": keep_alive,
            **({"status": outcome.status} if outcome.ok else {"detail": outcome.detail}),
        },
    )
    await db.commit()
    return outcome


async def unload_ollama_model(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    provider_id: UUID,
    metrics: JhinMetrics,
    tracer: Tracer,
    *,
    model: str,
    request_id: UUID,
    ip_hash: str,
) -> OllamaLoadOutcome:
    """Drop ``model`` from the host's memory. Unloading is quick, so it is
    awaited in full."""
    provider = await service.get_provider(db, ctx.workspace_id, provider_id)
    _require_ollama(provider)
    outcome: OllamaLoadOutcome
    try:
        client, native = await _native(db, crypto, ctx, provider, metrics, tracer)
    except (ProviderConfigError, ModelProviderError) as exc:
        outcome = OllamaLoadOutcome(
            ok=False, status="failed", model=model, keep_alive=None, detail=_describe(exc)
        )
    else:
        try:
            # Ollama answers an unload for a model that is not resident with the
            # same 200 it gives a real one, so the only way to say what actually
            # happened is to look first.
            resident = {
                loaded.name
                for loaded in await asyncio.wait_for(
                    native.loaded_models(), OLLAMA_LIST_TIMEOUT_SECONDS
                )
            }
            await asyncio.wait_for(native.unload_model(model), OLLAMA_LIST_TIMEOUT_SECONDS)
        except (ModelProviderError, TimeoutError) as exc:
            outcome = OllamaLoadOutcome(
                ok=False, status="failed", model=model, keep_alive=None, detail=_describe(exc)
            )
        else:
            outcome = OllamaLoadOutcome(
                ok=True,
                status="unloaded",
                model=model,
                keep_alive=None,
                detail=(
                    f"{model} was unloaded."
                    if model in resident
                    else f"{model} was not loaded; nothing to unload."
                ),
            )
        finally:
            await client.close()
    audit.record(
        db,
        action=(
            "provider.ollama_model_unloaded"
            if outcome.ok
            else "provider.ollama_model_unload_failed"
        ),
        target_type="model_provider",
        target_id=provider.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "display_name": provider.display_name,
            "model": model,
            **({} if outcome.ok else {"detail": outcome.detail}),
        },
    )
    await db.commit()
    return outcome
