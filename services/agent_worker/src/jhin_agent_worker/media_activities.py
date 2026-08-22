"""Activities behind AvatarGenerationWorkflow (experience design: media).

``generate_avatar`` reloads the queued generation row, resolves the declared
image-capable model profile, decrypts the provider credential at the moment
of use, renders the stored prompt (public identity only — it was built by the
API), pushes the bytes through the same safe normalizer as uploads, and
activates the asset atomically. ``fail_avatar_generation`` records a terminal
failure so the UI stops polling while the agent's previous avatar stays.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_db.models import (
    AuditEvent,
    AvatarGeneration,
    ModelProfile,
    ModelProvider,
)
from jhin_domain import (
    AVATAR_GENERATION_TERMINAL_STATUSES,
    ActorType,
    AvatarGenerationStatus,
    AvatarKind,
)
from jhin_media import (
    AgentNotFound,
    ImageRejected,
    MediaStore,
    PostgresMediaStore,
    activate_avatar,
    normalize_avatar,
)
from jhin_models import (
    ImageGenerationConfig,
    ImageGenerationUnsupported,
    ModelClient,
    ModelProviderError,
    as_image_generation_client,
    build_model_client,
)
from jhin_models.factory import ProviderConfigError
from jhin_observability import get_logger
from jhin_secrets import SecretStore
from jhin_secrets.redaction import redact_text
from jhin_workflows.avatar_generation import (
    ACTIVITY_FAIL_AVATAR_GENERATION,
    ACTIVITY_GENERATE_AVATAR,
    AvatarGenerationInput,
    FailAvatarGenerationInput,
    GenerateAvatarResult,
)

logger = get_logger(__name__)

ClientFactory = Callable[..., ModelClient]


def _default_client_factory(
    provider_type: str,
    *,
    base_url: str | None,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelClient:
    return build_model_client(
        provider_type, base_url=base_url, api_key=api_key, transport=transport
    )


class MediaActivities:
    def __init__(
        self,
        resources: Resources,
        *,
        store: MediaStore | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._resources = resources
        self._store: MediaStore = store or PostgresMediaStore()
        self._client_factory: ClientFactory = client_factory or _default_client_factory

    async def _load_generation(
        self, session: AsyncSession, workspace_id: UUID, generation_id: UUID
    ) -> AvatarGeneration:
        generation = await session.scalar(
            select(AvatarGeneration).where(
                AvatarGeneration.id == generation_id,
                AvatarGeneration.workspace_id == workspace_id,
            )
        )
        if generation is None:
            raise ApplicationError(
                "avatar generation not found", type="generation_not_found", non_retryable=True
            )
        return generation

    async def _resolve_provider(
        self, session: AsyncSession, generation: AvatarGeneration
    ) -> tuple[ModelProvider, ImageGenerationConfig]:
        profile = (
            await session.scalar(
                select(ModelProfile).where(
                    ModelProfile.id == generation.model_profile_id,
                    ModelProfile.workspace_id == generation.workspace_id,
                )
            )
            if generation.model_profile_id is not None
            else None
        )
        if profile is None:
            raise ApplicationError(
                "the model profile selected for this avatar no longer exists",
                type="image_generation_unsupported",
                non_retryable=True,
            )
        config = ImageGenerationConfig.from_profile_config(profile.config_json)
        if not config.enabled or not config.model:
            raise ApplicationError(
                "the selected model profile no longer allows image generation",
                type="image_generation_unsupported",
                non_retryable=True,
            )
        provider = await session.scalar(
            select(ModelProvider).where(
                ModelProvider.id == profile.provider_id,
                ModelProvider.workspace_id == generation.workspace_id,
            )
        )
        if provider is None or not provider.enabled:
            raise ApplicationError(
                "the model provider is disabled or missing",
                type="provider_disabled",
                non_retryable=True,
            )
        return provider, config

    async def _render(
        self, session: AsyncSession, generation: AvatarGeneration
    ) -> tuple[bytes, str, dict[str, Any]]:
        provider, config = await self._resolve_provider(session, generation)
        api_key: str | None = None
        if provider.secret_id is not None:
            api_key = await SecretStore(session, self._resources.crypto).reveal(
                generation.workspace_id, provider.secret_id
            )
        try:
            client = self._client_factory(
                provider.type, base_url=provider.base_url, api_key=api_key
            )
        except ProviderConfigError as exc:
            raise ApplicationError(
                redact_text(str(exc))[:2_000], type="provider_config", non_retryable=True
            ) from None
        try:
            try:
                images = as_image_generation_client(client)
            except ImageGenerationUnsupported as exc:
                raise ApplicationError(
                    redact_text(str(exc))[:500],
                    type="image_generation_unsupported",
                    non_retryable=True,
                ) from None
            try:
                generated = await images.generate_image(
                    generation.prompt, model=config.model, size=config.size
                )
            except ModelProviderError as exc:
                raise ApplicationError(
                    redact_text(str(exc))[:2_000],
                    type="provider_error",
                    non_retryable=not exc.retryable,
                ) from None
        finally:
            await client.close()
        return (
            generated.data,
            generated.content_type,
            {
                "model": generated.model or config.model,
                "provider_request_id": generated.provider_request_id,
                "latency_ms": generated.latency_ms,
            },
        )

    @activity.defn(name=ACTIVITY_GENERATE_AVATAR)
    async def generate_avatar_activity(self, params: AvatarGenerationInput) -> GenerateAvatarResult:
        workspace_id = UUID(params.workspace_id)
        generation_id = UUID(params.generation_id)
        async with self._resources.session_factory() as session:
            generation = await self._load_generation(session, workspace_id, generation_id)
            if (
                generation.status == AvatarGenerationStatus.SUCCEEDED.value
                and generation.result_asset_id is not None
            ):
                # Activity retry after a committed success: idempotent.
                return GenerateAvatarResult(asset_id=str(generation.result_asset_id), sha256="")
            if generation.status == AvatarGenerationStatus.FAILED.value:
                raise ApplicationError(
                    "generation already failed", type="generation_failed", non_retryable=True
                )
            generation.status = AvatarGenerationStatus.RUNNING.value
            generation.started_at = generation.started_at or datetime.now(UTC)
            await session.commit()

            data, content_type, provider_meta = await self._render(session, generation)
            try:
                normalized = normalize_avatar(data, declared_content_type=content_type)
            except ImageRejected as exc:
                raise ApplicationError(
                    f"generated image rejected ({exc.code}): {exc}",
                    type="image_rejected",
                    non_retryable=True,
                ) from None

            try:
                agent, asset, previous_asset_id = await activate_avatar(
                    session,
                    self._store,
                    workspace_id=workspace_id,
                    agent_id=UUID(params.agent_id),
                    normalized=normalized,
                    avatar_kind=AvatarKind.GENERATED,
                    created_by_user_id=generation.created_by_user_id,
                )
            except AgentNotFound:
                raise ApplicationError(
                    "agent no longer exists", type="agent_not_found", non_retryable=True
                ) from None
            now = datetime.now(UTC)
            generation.status = AvatarGenerationStatus.SUCCEEDED.value
            generation.result_asset_id = asset.id
            generation.finished_at = now
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action="agent.avatar.generated",
                    target_type="agent",
                    target_id=agent.id,
                    metadata_json={
                        "generation_id": str(generation.id),
                        "asset_id": str(asset.id),
                        "sha256": asset.sha256,
                        "replaced_asset_id": str(previous_asset_id) if previous_asset_id else None,
                        "provider_type": generation.provider_type,
                        "model_name": provider_meta["model"],
                        "provider_request_id": provider_meta["provider_request_id"],
                        "latency_ms": provider_meta["latency_ms"],
                        "estimated_cost_micros": generation.estimated_cost_micros,
                    },
                )
            )
            await session.commit()
            logger.info(
                "avatar.generated",
                workspace_id=str(workspace_id),
                agent_id=str(agent.id),
                generation_id=str(generation.id),
            )
            return GenerateAvatarResult(asset_id=str(asset.id), sha256=asset.sha256)

    @activity.defn(name=ACTIVITY_FAIL_AVATAR_GENERATION)
    async def fail_avatar_generation_activity(self, params: FailAvatarGenerationInput) -> None:
        workspace_id = UUID(params.workspace_id)
        async with self._resources.session_factory() as session:
            generation = await self._load_generation(
                session, workspace_id, UUID(params.generation_id)
            )
            if AvatarGenerationStatus(generation.status) in AVATAR_GENERATION_TERMINAL_STATUSES:
                return
            generation.status = AvatarGenerationStatus.FAILED.value
            generation.error_code = params.error_code[:64]
            generation.error = redact_text(params.error)[:2_000]
            generation.finished_at = datetime.now(UTC)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action="agent.avatar.generation_failed",
                    target_type="agent",
                    target_id=UUID(params.agent_id),
                    metadata_json={
                        "generation_id": str(generation.id),
                        "error_code": generation.error_code,
                        "provider_type": generation.provider_type,
                    },
                )
            )
            await session.commit()
