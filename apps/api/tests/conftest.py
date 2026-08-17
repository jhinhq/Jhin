"""Shared fixtures for API unit tests: in-memory SQLite schema, a workspace
admin context, and envelope crypto — enough to exercise service-layer logic
without the compose stack (integration tests cover the real stack)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_api.deps import WorkspaceContext
from jhin_db.base import Base
from jhin_db.models import User, Workspace
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_secrets import SecretCrypto
from jhin_secrets.crypto import MasterKey, decode_master_key_material, generate_master_key_material


@pytest.fixture
def crypto() -> SecretCrypto:
    return SecretCrypto(MasterKey(key=decode_master_key_material(generate_master_key_material())))


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def admin_ctx(session: AsyncSession) -> WorkspaceContext:
    user = User(
        email=f"admin-{new_uuid7().hex[:8]}@example.com",
        display_name="Admin",
        password_hash="x",
    )
    workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add_all([user, workspace])
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=workspace.id, role=WorkspaceRole.ADMIN)
