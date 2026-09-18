from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from jhin_api.runtime import service
from jhin_db.models import Conversation, RuntimeSession


@pytest.fixture
async def disk(session, admin_ctx, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORKSPACE_MAX_MB", "1")
    monkeypatch.setenv("SANDBOX_WORKSPACE_TOTAL_MAX_MB", "2")
    chat = Conversation(
        workspace_id=admin_ctx.workspace_id,
        title="Quota",
        last_activity_at=datetime.now(UTC),
        workspace_version=1,
    )
    session.add(chat)
    await session.flush()
    row = await service.workspace(session, admin_ctx, chat.id)
    row.holder_user_id = admin_ctx.user.id
    await session.commit()
    return row


@pytest.mark.parametrize(
    "action,payload",
    [
        ("files", {"operation": "write", "args": {"content_base64": "eA=="}}),
        ("start", {"session_id": "not-dispatched"}),
    ],
)
async def test_api_refuses_known_full_disk_before_minting_write_capability(
    session, admin_ctx, disk, monkeypatch, action, payload
):
    disk.size_bytes = 1024 * 1024 + 1
    await session.commit()
    post = AsyncMock(return_value=httpx.Response(200, json={}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(HTTPException) as error:
        await service.gateway_call(
            session, admin_ctx, disk.conversation_id, disk, action, payload, write=True
        )
    assert error.value.status_code == 507
    assert "preserved" in error.value.detail
    post.assert_not_awaited()
    assert await session.scalar(select(func.count()).select_from(RuntimeSession)) == 0


@pytest.mark.parametrize(
    "action,payload,write",
    [
        ("files", {"operation": "read", "args": {"path": "report.txt"}}, False),
        ("files", {"operation": "snapshot", "args": {}}, False),
        ("files", {"operation": "write", "args": {"content_base64": None}}, True),
        ("files", {"operation": "write", "args": {"content_base64": ""}}, True),
        ("stop", {}, True),
        ("interrupt", {}, True),
    ],
)
async def test_api_recovery_operations_remain_available_over_quota(
    session, admin_ctx, disk, monkeypatch, action, payload, write
):
    disk.size_bytes = 1024 * 1024 + 1
    disk.size_state = "unknown"
    await session.commit()
    post = AsyncMock(return_value=httpx.Response(200, json={"allowed": True}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await service.gateway_call(
        session, admin_ctx, disk.conversation_id, disk, action, payload, write=write
    ) == {"allowed": True}
    post.assert_awaited_once()


async def test_new_empty_chat_does_not_require_a_prior_measurement(
    session, admin_ctx, disk, monkeypatch
):
    assert disk.size_bytes == 0 and disk.size_measured_at is None
    post = AsyncMock(return_value=httpx.Response(200, json={}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await service.gateway_call(
        session,
        admin_ctx,
        disk.conversation_id,
        disk,
        "files",
        {"operation": "write", "args": {"content_base64": "eA=="}},
        write=True,
    )
    post.assert_awaited_once()


async def test_full_disk_can_return_workspace_control(session, admin_ctx, disk):
    disk.size_bytes = 1024 * 1024 + 1
    disk.size_state = "unknown"
    generation = disk.lease_generation
    await session.commit()
    await service.control(session, admin_ctx, disk.conversation_id, "return")
    await session.refresh(disk)
    assert disk.holder_user_id is None
    assert disk.lease_generation == generation + 1
