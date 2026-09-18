"""A user's API key cannot borrow the owner's broader downstream authority."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import ApiKey, Message, User, Workspace, WorkspaceMembership
from jhin_secrets.authority import attest_human_content, human_message_authorized


@pytest.fixture
async def state():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        workspace = Workspace(name="Test", slug=str(uuid4()))
        user = User(email="owner@example.test", display_name="Owner", password_hash="unused")
        db.add_all([workspace, user])
        await db.flush()
        membership = WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner")
        key = ApiKey(
            workspace_id=workspace.id,
            created_by_user_id=user.id,
            name="Limited",
            prefix="synthetic",
            key_hash="unused",
            role_ceiling="admin",
            scopes_json=["chats:write", "variables:write", "apps:write"],
        )
        db.add_all([membership, key])
        await db.flush()
        yield db, workspace, user, membership, key
    await engine.dispose()


def message(workspace, user, **kwargs):
    return Message(
        workspace_id=workspace.id,
        sender_type="user",
        sender_id=user.id,
        content_json=attest_human_content(
            {"text": "Save company-wide"},
            workspace_id=workspace.id,
            user_id=user.id,
            role="admin",
            **kwargs,
        ),
    )


async def test_admin_browser_is_current_and_unknown_legacy_is_not_authority(state):
    db, w, user, membership, _key = state
    row = message(w, user)
    assert await human_message_authorized(db, row, required_scope="variables:write")
    row.content_json = {"text": "Save company-wide"}
    assert not await human_message_authorized(db, row, required_scope="variables:write")
    row = message(w, user)
    row.sender_type = "agent"
    assert not await human_message_authorized(db, row, required_scope="variables:write")
    row.sender_type = "user"
    membership.role = "member"
    await db.flush()
    assert not await human_message_authorized(db, row, required_scope="variables:write")


@pytest.mark.parametrize(
    "change",
    [
        "initial_role",
        "initial_scope",
        "revoked",
        "expired",
        "current_role",
        "current_scope",
        "foreign_issuer",
        "foreign_workspace",
        "disabled_user",
    ],
)
async def test_api_key_cannot_be_promoted_or_used_after_revocation(state, change):
    db, w, user, _membership, key = state
    row = message(w, user, api_key_id=key.id, scopes=frozenset({"variables:write"}))
    assert await human_message_authorized(db, row, required_scope="variables:write")
    assert not await human_message_authorized(db, row, required_scope="apps:write")
    if change == "initial_role":
        row.content_json["_human_authority"]["role"] = "member"
    elif change == "initial_scope":
        row.content_json["_human_authority"]["scopes"] = ["chats:write"]
    elif change == "revoked":
        key.revoked_at = datetime.now(UTC)
    elif change == "expired":
        key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "current_role":
        key.role_ceiling = "member"
    elif change == "current_scope":
        key.scopes_json = ["chats:write"]
    elif change == "foreign_issuer":
        key.created_by_user_id = uuid4()
    elif change == "foreign_workspace":
        key.workspace_id = uuid4()
    elif change == "disabled_user":
        user.status = "disabled"
    await db.flush()
    assert not await human_message_authorized(db, row, required_scope="variables:write")
