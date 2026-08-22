"""Avatar upload/removal, authenticated media delivery, and generation
requests against in-memory SQLite."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents.schemas import AgentOut
from jhin_api.deps import AuthContext, WorkspaceContext, get_current_auth, get_db
from jhin_api.media import service
from jhin_api.media.router import get_media_store
from jhin_api.media.router import router as media_router
from jhin_api.settings import Settings
from jhin_db.models import (
    Agent,
    AuditEvent,
    AvatarGeneration,
    MediaAsset,
    ModelProfile,
    ModelProvider,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import (
    AvatarGenerationStatus,
    AvatarKind,
    MediaAssetStatus,
    WorkspaceRole,
    new_uuid7,
)
from jhin_media import PostgresMediaStore


def _png(width: int = 96, height: int = 64, color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _meta() -> dict[str, Any]:
    return {"request_id": new_uuid7(), "ip_hash": "test-ip-hash"}


async def _agent(session: AsyncSession, workspace_id: UUID, name: str = "Ada Lovelace") -> Agent:
    agent = Agent(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{new_uuid7().hex[:6]}",
        role_title="Staff Engineer",
        public_purpose="Keeps the build green",
        expertise_json=["ci"],
    )
    session.add(agent)
    await session.flush()
    return agent


async def _other_workspace(session: AsyncSession) -> Workspace:
    workspace = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(workspace)
    await session.flush()
    return workspace


async def _image_profile(
    session: AsyncSession, workspace_id: UUID, *, enabled: bool = True
) -> ModelProfile:
    provider = ModelProvider(
        workspace_id=workspace_id,
        type="openai_compatible",
        display_name=f"Fake {new_uuid7().hex[:4]}",
        base_url="http://fake.invalid/v1",
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=workspace_id,
        provider_id=provider.id,
        model_name="fake-mini",
        display_name=f"Images {new_uuid7().hex[:4]}",
        config_json=(
            {
                "image_generation": {
                    "enabled": True,
                    "model": "fake-image",
                    "cost_micros": 40_000,
                }
            }
            if enabled
            else {}
        ),
    )
    session.add(profile)
    await session.flush()
    return profile


class _TemporalClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started: list[dict[str, Any]] = []

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str) -> None:
        if self.fail:
            raise OSError("temporal down")
        self.started.append({"name": name, "arg": arg, "id": id, "task_queue": task_queue})


# --- upload / remove ---------------------------------------------------------


async def test_upload_activates_atomically_and_audits(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    assert agent.avatar_kind == AvatarKind.INITIALS.value

    updated = await service.upload_avatar(
        session,
        admin_ctx,
        store,
        agent.id,
        data=_png(),
        declared_content_type="image/png",
        **_meta(),
    )
    assert updated.avatar_kind == AvatarKind.UPLOAD.value
    assert updated.active_avatar_asset_id is not None
    asset = await session.get(MediaAsset, updated.active_avatar_asset_id)
    assert asset is not None
    assert asset.status == MediaAssetStatus.ACTIVE.value
    assert asset.owner_agent_id == agent.id
    assert asset.created_by_user_id == admin_ctx.user.id
    assert asset.width == asset.height == 256
    assert len(asset.variant_64) and len(asset.variant_128) and len(asset.variant_256)
    audit = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "agent.avatar.uploaded")
    )
    assert audit is not None and audit.target_id == agent.id
    assert audit.metadata_json["asset_id"] == str(asset.id)

    out = AgentOut.model_validate(updated, from_attributes=True)
    assert out.avatar_kind == AvatarKind.UPLOAD
    assert out.avatar_url == f"/api/v1/workspaces/{admin_ctx.workspace_id}/media/{asset.id}"
    assert service.avatar_out(updated).initials == "AL"


async def test_replacement_retires_previous_and_failed_upload_keeps_it(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    first = await service.upload_avatar(
        session, admin_ctx, store, agent.id, data=_png(), declared_content_type=None, **_meta()
    )
    first_asset_id = first.active_avatar_asset_id
    assert first_asset_id is not None

    with pytest.raises(HTTPException) as rejected:
        await service.upload_avatar(
            session,
            admin_ctx,
            store,
            agent.id,
            data=b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            declared_content_type="image/svg+xml",
            **_meta(),
        )
    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "unsupported_format"  # type: ignore[index]
    await session.refresh(agent)
    assert agent.active_avatar_asset_id == first_asset_id
    assert agent.avatar_kind == AvatarKind.UPLOAD.value

    second = await service.upload_avatar(
        session,
        admin_ctx,
        store,
        agent.id,
        data=_png(color=(10, 200, 30)),
        declared_content_type="image/png",
        **_meta(),
    )
    assert second.active_avatar_asset_id != first_asset_id
    previous = await session.get(MediaAsset, first_asset_id)
    assert previous is not None
    assert previous.status == MediaAssetStatus.RETIRED.value
    assert previous.retired_at is not None
    assert await store.get_variant(session, admin_ctx.workspace_id, first_asset_id, 64) is None


async def test_mime_mismatch_is_422(session: AsyncSession, admin_ctx: WorkspaceContext) -> None:
    agent = await _agent(session, admin_ctx.workspace_id)
    with pytest.raises(HTTPException) as excinfo:
        await service.upload_avatar(
            session,
            admin_ctx,
            PostgresMediaStore(),
            agent.id,
            data=_png(),
            declared_content_type="image/jpeg",
            **_meta(),
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "content_type_mismatch"  # type: ignore[index]


async def test_remove_returns_to_initials_and_is_idempotent(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    uploaded = await service.upload_avatar(
        session, admin_ctx, store, agent.id, data=_png(), declared_content_type=None, **_meta()
    )
    asset_id = uploaded.active_avatar_asset_id
    removed = await service.remove_avatar(session, admin_ctx, store, agent.id, **_meta())
    assert removed.avatar_kind == AvatarKind.INITIALS.value
    assert removed.active_avatar_asset_id is None
    assert service.avatar_out(removed).avatar_url is None
    assert asset_id is not None
    assert await store.get_variant(session, admin_ctx.workspace_id, asset_id, 128) is None
    again = await service.remove_avatar(session, admin_ctx, store, agent.id, **_meta())
    assert again.active_avatar_asset_id is None
    audits = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "agent.avatar.removed"))
    )
    assert len(audits) == 2


async def test_cross_workspace_agent_and_media_are_404(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    other = await _other_workspace(session)
    foreign_agent = await _agent(session, other.id, "Foreign")
    foreign_ctx = WorkspaceContext(
        user=admin_ctx.user, workspace_id=other.id, role=WorkspaceRole.ADMIN
    )
    uploaded = await service.upload_avatar(
        session,
        foreign_ctx,
        store,
        foreign_agent.id,
        data=_png(),
        declared_content_type=None,
        **_meta(),
    )
    foreign_asset = uploaded.active_avatar_asset_id
    assert foreign_asset is not None

    with pytest.raises(HTTPException) as upload:
        await service.upload_avatar(
            session,
            admin_ctx,
            store,
            foreign_agent.id,
            data=_png(),
            declared_content_type=None,
            **_meta(),
        )
    assert upload.value.status_code == 404
    with pytest.raises(HTTPException) as remove:
        await service.remove_avatar(session, admin_ctx, store, foreign_agent.id, **_meta())
    assert remove.value.status_code == 404
    with pytest.raises(HTTPException) as media:
        await service.get_media_variant(session, store, admin_ctx.workspace_id, foreign_asset, 64)
    assert media.value.status_code == 404
    with pytest.raises(HTTPException) as generation:
        await service.latest_generation(session, admin_ctx.workspace_id, foreign_agent.id)
    assert generation.value.status_code == 404
    # Still readable from its own workspace.
    variant = await service.get_media_variant(session, store, other.id, foreign_asset, 64)
    assert variant.content_type == "image/webp"


# --- generation --------------------------------------------------------------


async def test_generation_request_builds_public_prompt_and_starts_workflow(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx.workspace_id)
    agent.system_prompt = "TOP-SECRET-INSTRUCTIONS"
    profile = await _image_profile(session, admin_ctx.workspace_id)
    temporal = _TemporalClient()

    generation = await service.request_generation(
        session,
        admin_ctx,
        temporal,
        agent.id,
        prompt_hint="warm colors",
        **_meta(),  # type: ignore[arg-type]
    )
    assert generation.status == AvatarGenerationStatus.QUEUED.value
    assert generation.model_profile_id == profile.id
    assert generation.model_name == "fake-image"
    assert generation.estimated_cost_micros == 40_000
    assert generation.temporal_workflow_id == f"avatar-generation-{generation.id}"
    assert "Ada Lovelace" in generation.prompt
    assert "warm colors" in generation.prompt
    assert "TOP-SECRET-INSTRUCTIONS" not in generation.prompt
    assert len(temporal.started) == 1
    assert temporal.started[0]["name"] == "AvatarGenerationWorkflow"
    assert temporal.started[0]["arg"].generation_id == str(generation.id)

    out = service.generation_out(generation)
    assert out.disclosure.provider_type == "openai_compatible"
    assert out.disclosure.estimated_cost_micros == 40_000
    assert out.result_avatar_url is None
    # Agent creation/identity is untouched until the worker activates a result.
    await session.refresh(agent)
    assert agent.avatar_kind == AvatarKind.INITIALS.value
    latest = await service.latest_generation(session, admin_ctx.workspace_id, agent.id)
    assert latest.id == generation.id

    with pytest.raises(HTTPException) as duplicate:
        await service.request_generation(
            session,
            admin_ctx,
            temporal,
            agent.id,
            prompt_hint="",
            **_meta(),  # type: ignore[arg-type]
        )
    assert duplicate.value.status_code == 409
    assert duplicate.value.detail["code"] == "generation_in_progress"  # type: ignore[index]


async def test_generation_without_capable_profile_is_409(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx.workspace_id)
    await _image_profile(session, admin_ctx.workspace_id, enabled=False)
    with pytest.raises(HTTPException) as excinfo:
        await service.request_generation(
            session,
            admin_ctx,
            _TemporalClient(),
            agent.id,
            prompt_hint="",
            **_meta(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "image_generation_unsupported"  # type: ignore[index]
    assert await session.scalar(select(AvatarGeneration.id)) is None


async def test_generation_dispatch_failure_marks_row_failed(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    agent = await _agent(session, admin_ctx.workspace_id)
    await _image_profile(session, admin_ctx.workspace_id)
    with pytest.raises(HTTPException) as excinfo:
        await service.request_generation(
            session,
            admin_ctx,
            _TemporalClient(fail=True),
            agent.id,
            prompt_hint="",
            **_meta(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 503
    row = await session.scalar(select(AvatarGeneration))
    assert row is not None
    assert row.status == AvatarGenerationStatus.FAILED.value
    assert row.error_code == "dispatch_failed"


# --- HTTP delivery -----------------------------------------------------------


@pytest.fixture
async def media_client(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, Any]]]:
    membership = WorkspaceMembership(
        workspace_id=admin_ctx.workspace_id, user_id=admin_ctx.user.id, role="admin"
    )
    viewer = User(
        email=f"viewer-{new_uuid7().hex[:6]}@example.com", display_name="V", password_hash="x"
    )
    session.add_all([membership, viewer])
    await session.flush()
    session.add(
        WorkspaceMembership(workspace_id=admin_ctx.workspace_id, user_id=viewer.id, role="viewer")
    )
    other = await _other_workspace(session)
    await session.commit()

    app = FastAPI()
    app.state.settings = Settings(csrf_cookie_name="csrf", csrf_header_name="x-csrf-token")

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.state.media_store = PostgresMediaStore()
    app.include_router(media_router)
    current = {"user": admin_ctx.user}

    async def _db() -> AsyncIterator[AsyncSession]:
        yield session

    async def _auth() -> AuthContext:
        return AuthContext(user=current["user"], session_record=UserSession())  # type: ignore[call-arg]

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_auth] = _auth
    app.dependency_overrides[get_media_store] = lambda: app.state.media_store
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, {"current": current, "viewer": viewer, "other": other}


async def test_media_delivery_headers_and_auth(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    media_client: tuple[httpx.AsyncClient, dict[str, Any]],
) -> None:
    client, world = media_client
    ws = admin_ctx.workspace_id
    agent = await _agent(session, ws)
    await session.commit()
    csrf = {"x-csrf-token": "tok"}
    client.cookies.set("csrf", "tok")

    missing_csrf = await client.post(
        f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar",
        files={"file": ("a.png", _png(), "image/png")},
    )
    assert missing_csrf.status_code == 403
    uploaded = await client.post(
        f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar",
        files={"file": ("a.png", _png(), "image/png")},
        headers=csrf,
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["avatar_kind"] == "upload"
    asset_id = body["active_avatar_asset_id"]
    assert body["avatar_url"] == f"/api/v1/workspaces/{ws}/media/{asset_id}"

    default = await client.get(f"/api/v1/workspaces/{ws}/media/{asset_id}")
    assert default.status_code == 200
    assert default.headers["content-type"] == "image/webp"
    assert default.headers["cache-control"] == "private, max-age=86400, immutable"
    assert default.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(default.content)).size == (128, 128)
    small = await client.get(f"/api/v1/workspaces/{ws}/media/{asset_id}", params={"size": 64})
    assert Image.open(io.BytesIO(small.content)).size == (64, 64)
    etag = small.headers["etag"]
    cached = await client.get(
        f"/api/v1/workspaces/{ws}/media/{asset_id}",
        params={"size": 64},
        headers={"if-none-match": etag},
    )
    assert cached.status_code == 304
    bad_size = await client.get(f"/api/v1/workspaces/{ws}/media/{asset_id}", params={"size": 512})
    assert bad_size.status_code == 422

    # Viewer: may read media and the avatar card, may not change it.
    world["current"]["user"] = world["viewer"]
    viewer_read = await client.get(f"/api/v1/workspaces/{ws}/media/{asset_id}")
    assert viewer_read.status_code == 200
    card = await client.get(f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar")
    assert card.status_code == 200 and card.json()["initials"] == "AL"
    forbidden = await client.delete(
        f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar", headers=csrf
    )
    assert forbidden.status_code == 403
    no_generation = await client.get(f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar/generation")
    assert no_generation.status_code == 404

    # Foreign workspace: the viewer is not a member -> 404, never the bytes.
    other_ws = world["other"].id
    foreign = await client.get(f"/api/v1/workspaces/{other_ws}/media/{asset_id}")
    assert foreign.status_code == 404

    # CSRF: a mutating call without the header is rejected before any work.
    world["current"]["user"] = admin_ctx.user
    no_csrf = await client.delete(f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar")
    assert no_csrf.status_code == 403
    removed = await client.delete(f"/api/v1/workspaces/{ws}/agents/{agent.id}/avatar", headers=csrf)
    assert removed.status_code == 200 and removed.json()["avatar_url"] is None
    gone = await client.get(f"/api/v1/workspaces/{ws}/media/{asset_id}")
    assert gone.status_code == 404
