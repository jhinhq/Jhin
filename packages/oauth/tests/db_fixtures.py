"""Real database fixtures for the persistence and lifecycle suites.

A plain module rather than a second ``conftest.py``: this package's conftest
belongs to the protocol suite (fake authorization servers, a live HTTP
client), and the storage suite needs a different thing entirely — a real
schema, real envelope encryption, and real rows. Nothing here is mocked: the
encryption is the production ``SecretCrypto``, and every query runs against
SQLAlchemy's own SQLite backend so a broken column or constraint fails here
rather than in a migration review.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jhin_db.base import Base
from jhin_db.models import User, Workspace
from jhin_domain import new_uuid7
from jhin_secrets import SecretCrypto
from jhin_secrets.crypto import (
    MasterKey,
    decode_master_key_material,
    generate_master_key_material,
)


@dataclass(frozen=True)
class Tenant:
    """One workspace and the admin who authorizes things in it."""

    workspace_id: UUID
    user_id: UUID


@pytest.fixture
def crypto() -> SecretCrypto:
    return SecretCrypto(MasterKey(key=decode_master_key_material(generate_master_key_material())))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over one in-memory schema shared by every session.

    A shared cache is what lets the sweep open a session per connection — the
    behaviour that keeps one connection's failure from rolling back another's
    rotated refresh token — against the same database the test set up.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///file:jhin_oauth_tests?mode=memory&cache=shared&uri=true"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as db_session:
        yield db_session


@pytest.fixture
async def tenant(session: AsyncSession) -> Tenant:
    user = User(
        email=f"oauth-{new_uuid7().hex[:8]}@example.com",
        display_name="OAuth Admin",
        password_hash="x",
    )
    workspace = Workspace(name="OAuth", slug=f"oauth-{new_uuid7().hex[:8]}")
    session.add_all([user, workspace])
    await session.flush()
    await session.commit()
    return Tenant(workspace_id=workspace.id, user_id=user.id)
