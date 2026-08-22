"""Avatar generation activities against SQLite with a stub image provider:
success activates atomically; failure (provider error, rejected image,
unsupported profile) records the failure and keeps the previous avatar."""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker.media_activities import MediaActivities
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AuditEvent,
    AvatarGeneration,
    MediaAsset,
    ModelProfile,
    ModelProvider,
    Workspace,
)
from jhin_domain import AvatarGenerationStatus, AvatarKind, MediaAssetStatus, new_uuid7
from jhin_media import PostgresMediaStore, normalize_avatar
from jhin_media.avatars import activate_avatar
from jhin_models import GeneratedImage, ModelProviderError
from jhin_models.testing import deterministic_png
from jhin_workflows.avatar_generation import (
    AvatarGenerationInput,
    FailAvatarGenerationInput,
)


class StubResources:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory
        self.crypto = None


class StubImageClient:
    provider_name = "stub"

    def __init__(self, *, data: bytes | None = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.prompts: list[str] = []
        self.closed = False

    async def generate(self, request: Any) -> Any:  # pragma: no cover - not used
        raise NotImplementedError

    def stream(self, request: Any) -> Any:  # pragma: no cover - not used
        raise NotImplementedError

    async def verify(self) -> str:  # pragma: no cover - not used
        return "ok"

    async def close(self) -> None:
        self.closed = True

    async def generate_image(
        self, prompt: str, *, model: str, size: str = "1024x1024"
    ) -> GeneratedImage:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        assert self.data is not None
        return GeneratedImage(data=self.data, content_type="image/png", model=model)


class NoImagesClient(StubImageClient):
    """A chat-only provider adapter: no ``generate_image`` attribute."""

    generate_image = None  # type: ignore[assignment]


class World:
    workspace: Workspace
    agent: Agent
    generation: AvatarGeneration
    session_factory: Any
    previous_asset_id: UUID


async def _build_world(*, image_enabled: bool = True) -> World:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    world = World()
    world.session_factory = factory
    async with factory() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:6]}")
        session.add(workspace)
        await session.flush()
        provider = ModelProvider(
            workspace_id=workspace.id, type="openai_compatible", display_name="Fake"
        )
        session.add(provider)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_id=provider.id,
            model_name="fake-mini",
            display_name="Images",
            config_json=(
                {"image_generation": {"enabled": True, "model": "fake-image"}}
                if image_enabled
                else {}
            ),
        )
        agent = Agent(workspace_id=workspace.id, name="Ada", slug="ada", role_title="Engineer")
        session.add_all([profile, agent])
        await session.flush()
        # A previously uploaded avatar that must survive failed generations.
        buffer = io.BytesIO()
        Image.new("RGB", (80, 80), (1, 2, 3)).save(buffer, format="PNG")
        _agent, previous, _ = await activate_avatar(
            session,
            PostgresMediaStore(),
            workspace_id=workspace.id,
            agent_id=agent.id,
            normalized=normalize_avatar(buffer.getvalue()),
            avatar_kind=AvatarKind.UPLOAD,
            created_by_user_id=None,
        )
        generation = AvatarGeneration(
            workspace_id=workspace.id,
            agent_id=agent.id,
            prompt="stylized portrait of Ada",
            provider_type=provider.type,
            provider_display_name=provider.display_name,
            model_profile_id=profile.id,
            model_name="fake-image",
        )
        session.add(generation)
        await session.commit()
        world.workspace = workspace
        world.agent = agent
        world.generation = generation
        world.previous_asset_id = previous.id
    return world


def _input(world: World) -> AvatarGenerationInput:
    return AvatarGenerationInput(
        workspace_id=str(world.workspace.id),
        agent_id=str(world.agent.id),
        generation_id=str(world.generation.id),
    )


def _activities(world: World, client: StubImageClient) -> MediaActivities:
    return MediaActivities(
        StubResources(world.session_factory),  # type: ignore[arg-type]
        client_factory=lambda *_args, **_kwargs: client,
    )


async def test_success_normalizes_activates_and_audits() -> None:
    world = await _build_world()
    client = StubImageClient(data=deterministic_png("stylized portrait of Ada"))
    result = await ActivityEnvironment().run(
        _activities(world, client).generate_avatar_activity, _input(world)
    )
    assert client.prompts == ["stylized portrait of Ada"]
    assert client.closed
    async with world.session_factory() as session:
        agent = await session.get(Agent, world.agent.id)
        assert agent is not None
        assert agent.avatar_kind == AvatarKind.GENERATED.value
        assert str(agent.active_avatar_asset_id) == result.asset_id
        asset = await session.get(MediaAsset, UUID(result.asset_id))
        assert asset is not None and asset.status == MediaAssetStatus.ACTIVE.value
        assert asset.sha256 == result.sha256
        assert Image.open(io.BytesIO(asset.variant_256)).format == "WEBP"
        previous = await session.get(MediaAsset, world.previous_asset_id)
        assert previous is not None and previous.status == MediaAssetStatus.RETIRED.value
        generation = await session.get(AvatarGeneration, world.generation.id)
        assert generation is not None
        assert generation.status == AvatarGenerationStatus.SUCCEEDED.value
        assert generation.result_asset_id == asset.id
        assert generation.started_at is not None and generation.finished_at is not None
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "agent.avatar.generated")
        )
        assert audit is not None and audit.metadata_json["asset_id"] == str(asset.id)

    # Re-running after success is idempotent (activity retry after commit).
    again = await ActivityEnvironment().run(
        _activities(world, client).generate_avatar_activity, _input(world)
    )
    assert again.asset_id == result.asset_id
    assert len(client.prompts) == 1


async def _assert_previous_avatar_intact(world: World) -> None:
    async with world.session_factory() as session:
        agent = await session.get(Agent, world.agent.id)
        assert agent is not None
        assert agent.active_avatar_asset_id == world.previous_asset_id
        assert agent.avatar_kind == AvatarKind.UPLOAD.value
        previous = await session.get(MediaAsset, world.previous_asset_id)
        assert previous is not None and previous.status == MediaAssetStatus.ACTIVE.value
        assert (await session.scalar(select(AuditEvent))) is None


async def test_provider_error_keeps_previous_avatar() -> None:
    world = await _build_world()
    client = StubImageClient(error=ModelProviderError("HTTP 503", status_code=503, retryable=True))
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(world, client).generate_avatar_activity, _input(world)
        )
    assert excinfo.value.type == "provider_error"
    assert excinfo.value.non_retryable is False
    await _assert_previous_avatar_intact(world)


async def test_rejected_generated_image_keeps_previous_avatar() -> None:
    world = await _build_world()
    client = StubImageClient(data=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(world, client).generate_avatar_activity, _input(world)
        )
    assert excinfo.value.type == "image_rejected"
    assert excinfo.value.non_retryable is True
    await _assert_previous_avatar_intact(world)


async def test_chat_only_provider_is_unsupported() -> None:
    world = await _build_world()
    client = NoImagesClient()
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(world, client).generate_avatar_activity, _input(world)
        )
    assert excinfo.value.type == "image_generation_unsupported"
    assert client.closed
    await _assert_previous_avatar_intact(world)


async def test_profile_without_image_config_is_unsupported() -> None:
    world = await _build_world(image_enabled=False)
    client = StubImageClient(data=deterministic_png("x"))
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(world, client).generate_avatar_activity, _input(world)
        )
    assert excinfo.value.type == "image_generation_unsupported"
    assert client.prompts == []


async def test_fail_activity_records_terminal_failure_once() -> None:
    world = await _build_world()
    activities = _activities(world, StubImageClient())
    params = FailAvatarGenerationInput(
        workspace_id=str(world.workspace.id),
        agent_id=str(world.agent.id),
        generation_id=str(world.generation.id),
        error_code="provider_error",
        error="HTTP 503 from provider",
    )
    await ActivityEnvironment().run(activities.fail_avatar_generation_activity, params)
    await ActivityEnvironment().run(activities.fail_avatar_generation_activity, params)
    async with world.session_factory() as session:
        generation = await session.get(AvatarGeneration, world.generation.id)
        assert generation is not None
        assert generation.status == AvatarGenerationStatus.FAILED.value
        assert generation.error_code == "provider_error"
        assert generation.error == "HTTP 503 from provider"
        assert generation.finished_at is not None
        agent = await session.get(Agent, world.agent.id)
        assert agent is not None and agent.active_avatar_asset_id == world.previous_asset_id
        audits = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "agent.avatar.generation_failed")
            )
        )
        assert len(audits) == 1


async def test_unknown_generation_is_non_retryable() -> None:
    world = await _build_world()
    params = AvatarGenerationInput(
        workspace_id=str(world.workspace.id),
        agent_id=str(world.agent.id),
        generation_id=str(new_uuid7()),
    )
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(world, StubImageClient()).generate_avatar_activity, params
        )
    assert excinfo.value.type == "generation_not_found"
