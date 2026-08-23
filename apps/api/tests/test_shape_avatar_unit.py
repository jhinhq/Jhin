"""Free shape avatars: fixed-list validation, activation, audit, and reset."""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service as agents_service
from jhin_api.agents.schemas import AgentCreate, AgentOut
from jhin_api.deps import WorkspaceContext
from jhin_api.media import service
from jhin_api.media.schemas import AvatarShapeRequest
from jhin_db.models import Agent, AuditEvent, MediaAsset, Workspace
from jhin_domain import (
    AVATAR_COLORS,
    AVATAR_SHAPES,
    AvatarKind,
    MediaAssetStatus,
    WorkspaceRole,
    new_uuid7,
)
from jhin_media import PostgresMediaStore


def _png(width: int = 96, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 40, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _meta() -> dict[str, Any]:
    return {"request_id": new_uuid7(), "ip_hash": "test-ip-hash"}


async def _agent(session: AsyncSession, workspace_id: UUID, name: str = "Bisby Bot") -> Agent:
    agent = Agent(
        workspace_id=workspace_id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{new_uuid7().hex[:6]}",
        role_title="Helper",
    )
    session.add(agent)
    await session.flush()
    return agent


# --- request validation ------------------------------------------------------


def test_shape_request_validates_against_fixed_lists() -> None:
    ok = AvatarShapeRequest(shape="jay", color="#7371FC")
    assert ok.shape == "jay"
    assert ok.color == "#7371fc"  # normalized to lowercase
    with pytest.raises(ValidationError):
        AvatarShapeRequest(shape="hexagon", color="#7371fc")
    with pytest.raises(ValidationError):
        AvatarShapeRequest(shape="cube", color="#123456")
    with pytest.raises(ValidationError):
        AvatarShapeRequest(shape="cube", color="rebeccapurple")
    assert len(AVATAR_SHAPES) == 8
    assert len(AVATAR_COLORS) == 12
    assert all(color.startswith("#") and len(color) == 7 for color in AVATAR_COLORS)


def test_agent_create_requires_shape_and_color_together() -> None:
    created = AgentCreate(name="Bisby", avatar_shape="cube", avatar_color="#7371FC")
    assert created.avatar_color == "#7371fc"
    assert AgentCreate(name="Plain").avatar_shape is None
    with pytest.raises(ValidationError):
        AgentCreate(name="Bisby", avatar_shape="cube")
    with pytest.raises(ValidationError):
        AgentCreate(name="Bisby", avatar_color="#7371fc")
    with pytest.raises(ValidationError):
        AgentCreate(name="Bisby", avatar_shape="blob", avatar_color="#7371fc")
    with pytest.raises(ValidationError):
        AgentCreate(name="Bisby", avatar_shape="cube", avatar_color="#00ff00")


# --- service -----------------------------------------------------------------


async def test_set_shape_avatar_sets_kind_and_audits(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    assert agent.avatar_kind == AvatarKind.INITIALS.value

    updated = await service.set_shape_avatar(
        session, admin_ctx, store, agent.id, shape="jay", color="#2e7558", **_meta()
    )
    assert updated.avatar_kind == AvatarKind.SHAPE.value
    assert updated.avatar_shape == "jay"
    assert updated.avatar_color == "#2e7558"
    assert updated.active_avatar_asset_id is None

    out = service.avatar_out(updated)
    assert out.avatar_shape == "jay"
    assert out.avatar_color == "#2e7558"
    assert out.avatar_url is None

    agent_out = AgentOut.model_validate(updated, from_attributes=True)
    assert agent_out.avatar_shape == "jay"
    assert agent_out.avatar_color == "#2e7558"
    assert agent_out.avatar_url is None

    audit_row = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "agent.avatar.shape_set")
    )
    assert audit_row is not None
    assert audit_row.target_id == agent.id
    assert audit_row.metadata_json["shape"] == "jay"
    assert audit_row.metadata_json["color"] == "#2e7558"
    assert audit_row.metadata_json["removed_asset_id"] is None


async def test_shape_replaces_uploaded_picture_and_retires_asset(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    uploaded = await service.upload_avatar(
        session, admin_ctx, store, agent.id, data=_png(), declared_content_type=None, **_meta()
    )
    asset_id = uploaded.active_avatar_asset_id
    assert asset_id is not None

    shaped = await service.set_shape_avatar(
        session, admin_ctx, store, agent.id, shape="quad", color="#3ecf8e", **_meta()
    )
    assert shaped.avatar_kind == AvatarKind.SHAPE.value
    assert shaped.active_avatar_asset_id is None
    previous = await session.get(MediaAsset, asset_id)
    assert previous is not None
    assert previous.status == MediaAssetStatus.RETIRED.value
    audit_row = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "agent.avatar.shape_set")
    )
    assert audit_row is not None
    assert audit_row.metadata_json["removed_asset_id"] == str(asset_id)


async def test_remove_resets_shape_to_initials(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    store = PostgresMediaStore()
    agent = await _agent(session, admin_ctx.workspace_id)
    await service.set_shape_avatar(
        session, admin_ctx, store, agent.id, shape="ess", color="#985b08", **_meta()
    )
    removed = await service.remove_avatar(session, admin_ctx, store, agent.id, **_meta())
    assert removed.avatar_kind == AvatarKind.INITIALS.value
    assert removed.avatar_shape is None
    assert removed.avatar_color is None
    out = service.avatar_out(removed)
    assert out.avatar_shape is None
    assert out.avatar_color is None


async def test_cross_workspace_shape_is_404(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    foreign = await _agent(session, other.id, "Foreign")
    with pytest.raises(HTTPException) as excinfo:
        await service.set_shape_avatar(
            session,
            admin_ctx,
            PostgresMediaStore(),
            foreign.id,
            shape="cube",
            color="#7371fc",
            **_meta(),
        )
    assert excinfo.value.status_code == 404
    # Still settable from its own workspace.
    foreign_ctx = WorkspaceContext(
        user=admin_ctx.user, workspace_id=other.id, role=WorkspaceRole.ADMIN
    )
    updated = await service.set_shape_avatar(
        session,
        foreign_ctx,
        PostgresMediaStore(),
        foreign.id,
        shape="cube",
        color="#7371fc",
        **_meta(),
    )
    assert updated.avatar_kind == AvatarKind.SHAPE.value


async def test_create_agent_with_shape_starts_as_shape_kind(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    payload = AgentCreate(name="Cubist", avatar_shape="tee", avatar_color="#b44351")
    created = await agents_service.create_agent(
        session,
        admin_ctx,
        values=payload.model_dump(),
        request_id=new_uuid7(),
        ip_hash="test-ip-hash",
    )
    assert created.avatar_kind == AvatarKind.SHAPE.value
    assert created.avatar_shape == "tee"
    assert created.avatar_color == "#b44351"
    plain = await agents_service.create_agent(
        session,
        admin_ctx,
        values=AgentCreate(name="Plain").model_dump(),
        request_id=new_uuid7(),
        ip_hash="test-ip-hash",
    )
    assert plain.avatar_kind == AvatarKind.INITIALS.value
