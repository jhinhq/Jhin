"""Avatar upload/removal, authenticated media reads, and asynchronous
generation requests. Every query is workspace-scoped; unknown or foreign ids
are 404 so nothing leaks across workspaces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError
from temporalio.service import RPCError

from jhin_api.agents.service import get_agent
from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.media.schemas import (
    AvatarGenerationOut,
    AvatarOut,
    ProviderDisclosure,
)
from jhin_api.media.urls import avatar_url_for
from jhin_db.models import Agent, AvatarGeneration, ModelProfile, ModelProvider, Workspace
from jhin_domain import (
    AvatarGenerationStatus,
    AvatarKind,
)
from jhin_media import (
    ImageRejected,
    MediaStore,
    StoredVariant,
    activate_avatar,
    build_avatar_prompt,
    clear_avatar,
    normalize_avatar,
)
from jhin_models import ImageGenerationConfig
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.avatar_generation import (
    AvatarGenerationInput,
    avatar_generation_workflow_id,
)


def initials_for(name: str) -> str:
    words = [word for word in name.replace("-", " ").replace("_", " ").split() if word]
    letters = [word[0] for word in words[:2]]
    return "".join(letters).upper() or "?"


def avatar_out(agent: Agent) -> AvatarOut:
    return AvatarOut(
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        avatar_kind=AvatarKind(agent.avatar_kind),
        active_avatar_asset_id=agent.active_avatar_asset_id,
        avatar_url=avatar_url_for(agent.workspace_id, agent.active_avatar_asset_id),
        initials=initials_for(agent.name),
    )


def generation_out(generation: AvatarGeneration) -> AvatarGenerationOut:
    return AvatarGenerationOut(
        id=generation.id,
        workspace_id=generation.workspace_id,
        agent_id=generation.agent_id,
        status=AvatarGenerationStatus(generation.status),
        prompt=generation.prompt,
        prompt_hint=generation.prompt_hint,
        disclosure=ProviderDisclosure(
            provider_type=generation.provider_type,
            provider_display_name=generation.provider_display_name,
            model_profile_id=generation.model_profile_id,
            model_name=generation.model_name,
            image_size=generation.image_size,
            estimated_cost_micros=generation.estimated_cost_micros,
        ),
        error=generation.error,
        error_code=generation.error_code,
        result_asset_id=generation.result_asset_id,
        result_avatar_url=avatar_url_for(generation.workspace_id, generation.result_asset_id),
        temporal_workflow_id=generation.temporal_workflow_id,
        created_at=generation.created_at,
        started_at=generation.started_at,
        finished_at=generation.finished_at,
        updated_at=generation.updated_at,
    )


async def upload_avatar(
    db: AsyncSession,
    ctx: WorkspaceContext,
    store: MediaStore,
    agent_id: UUID,
    *,
    data: bytes,
    declared_content_type: str | None,
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    await get_agent(db, ctx.workspace_id, agent_id)
    try:
        normalized = normalize_avatar(data, declared_content_type=declared_content_type)
    except ImageRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    agent, asset, previous_asset_id = await activate_avatar(
        db,
        store,
        workspace_id=ctx.workspace_id,
        agent_id=agent_id,
        normalized=normalized,
        avatar_kind=AvatarKind.UPLOAD,
        created_by_user_id=ctx.user.id,
    )
    audit.record(
        db,
        action="agent.avatar.uploaded",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "asset_id": str(asset.id),
            "sha256": asset.sha256,
            "source_format": normalized.source_format,
            "replaced_asset_id": str(previous_asset_id) if previous_asset_id else None,
        },
    )
    await db.commit()
    return agent


async def remove_avatar(
    db: AsyncSession,
    ctx: WorkspaceContext,
    store: MediaStore,
    agent_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> Agent:
    await get_agent(db, ctx.workspace_id, agent_id)
    agent, previous_asset_id = await clear_avatar(
        db, store, workspace_id=ctx.workspace_id, agent_id=agent_id
    )
    audit.record(
        db,
        action="agent.avatar.removed",
        target_type="agent",
        target_id=agent.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"removed_asset_id": str(previous_asset_id) if previous_asset_id else None},
    )
    await db.commit()
    return agent


async def get_media_variant(
    db: AsyncSession, store: MediaStore, workspace_id: UUID, asset_id: UUID, size: int
) -> StoredVariant:
    variant = await store.get_variant(db, workspace_id, asset_id, size)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return variant


async def _image_capable_profile(
    db: AsyncSession, workspace_id: UUID, agent: Agent
) -> tuple[ModelProfile, ModelProvider, ImageGenerationConfig]:
    """Pick the profile that renders avatars: the agent's own profile, then the
    workspace default, then any enabled profile in the workspace that declares
    ``config_json.image_generation.enabled``."""
    candidates: list[UUID] = []
    if agent.model_profile_id is not None:
        candidates.append(agent.model_profile_id)
    workspace = await db.get(Workspace, workspace_id)
    if workspace is not None and workspace.default_model_profile_id is not None:
        candidates.append(workspace.default_model_profile_id)
    ordered = list(
        await db.scalars(
            select(ModelProfile)
            .where(ModelProfile.workspace_id == workspace_id)
            .order_by(ModelProfile.created_at, ModelProfile.id)
        )
    )
    by_id = {profile.id: profile for profile in ordered}
    preferred = [by_id[pid] for pid in candidates if pid in by_id]
    for profile in preferred + [p for p in ordered if p.id not in candidates]:
        config = ImageGenerationConfig.from_profile_config(profile.config_json)
        if not config.enabled or not config.model:
            continue
        provider = await db.scalar(
            select(ModelProvider).where(
                ModelProvider.id == profile.provider_id,
                ModelProvider.workspace_id == workspace_id,
                ModelProvider.enabled.is_(True),
            )
        )
        if provider is None:
            continue
        return profile, provider, config
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "image_generation_unsupported",
            "message": "No enabled model profile in this workspace declares "
            "config_json.image_generation; avatars can still be uploaded.",
        },
    )


async def request_generation(
    db: AsyncSession,
    ctx: WorkspaceContext,
    temporal: TemporalClient,
    agent_id: UUID,
    *,
    prompt_hint: str,
    request_id: UUID,
    ip_hash: str,
) -> AvatarGeneration:
    agent = await get_agent(db, ctx.workspace_id, agent_id)
    in_flight = await db.scalar(
        select(AvatarGeneration.id).where(
            AvatarGeneration.workspace_id == ctx.workspace_id,
            AvatarGeneration.agent_id == agent_id,
            AvatarGeneration.status.in_(
                [AvatarGenerationStatus.QUEUED.value, AvatarGenerationStatus.RUNNING.value]
            ),
        )
    )
    if in_flight is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "generation_in_progress",
                "message": "An avatar is already being generated for this agent",
            },
        )
    profile, provider, config = await _image_capable_profile(db, ctx.workspace_id, agent)
    generation = AvatarGeneration(
        workspace_id=ctx.workspace_id,
        agent_id=agent.id,
        prompt=build_avatar_prompt(
            name=agent.name,
            role_title=agent.role_title,
            public_purpose=agent.public_purpose,
            expertise=list(agent.expertise_json or []),
            prompt_hint=prompt_hint,
        ),
        prompt_hint=prompt_hint,
        provider_type=provider.type,
        provider_display_name=provider.display_name,
        model_profile_id=profile.id,
        model_name=config.model,
        image_size=config.size,
        estimated_cost_micros=config.cost_micros,
        status=AvatarGenerationStatus.QUEUED.value,
        created_by_user_id=ctx.user.id,
    )
    db.add(generation)
    await db.flush()
    audit.record(
        db,
        action="agent.avatar.generation_requested",
        target_type="avatar_generation",
        target_id=generation.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={
            "agent_id": str(agent.id),
            "provider_type": provider.type,
            "model_name": config.model,
            "estimated_cost_micros": config.cost_micros,
        },
    )
    # Commit first: the row is the durable record even if dispatch fails.
    await db.commit()

    workflow_id = avatar_generation_workflow_id(str(generation.id))
    try:
        await temporal.start_workflow(
            "AvatarGenerationWorkflow",
            AvatarGenerationInput(
                workspace_id=str(ctx.workspace_id),
                agent_id=str(agent.id),
                generation_id=str(generation.id),
            ),
            id=workflow_id,
            task_queue=AGENT_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        pass  # Deterministic id: a retried request is already rendering.
    except (RPCError, TemporalError, OSError) as exc:
        generation.status = AvatarGenerationStatus.FAILED.value
        generation.error_code = "dispatch_failed"
        generation.error = "Could not start the generation workflow (Temporal unavailable)"
        generation.finished_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start avatar generation (Temporal unavailable)",
        ) from exc
    generation.temporal_workflow_id = workflow_id
    await db.commit()
    return generation


async def latest_generation(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> AvatarGeneration:
    await get_agent(db, workspace_id, agent_id)
    generation = await db.scalar(
        select(AvatarGeneration)
        .where(
            AvatarGeneration.workspace_id == workspace_id,
            AvatarGeneration.agent_id == agent_id,
        )
        .order_by(AvatarGeneration.created_at.desc(), AvatarGeneration.id.desc())
        .limit(1)
    )
    if generation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No avatar generation for this agent"
        )
    return generation
