"""Shared fixtures: in-memory SQLite schema, a workspace with a stored
connection (encrypted credential), and an execution context holding crypto —
the same shape connector executors see inside the agent worker."""

import json
from collections.abc import AsyncIterator, Awaitable
from typing import Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Connection, Workspace
from jhin_domain import SecretType, new_uuid7
from jhin_secrets import SecretCrypto, SecretStore
from jhin_secrets.crypto import MasterKey, decode_master_key_material, generate_master_key_material
from jhin_tools.builtin import ToolExecutionContext


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
async def workspace(session: AsyncSession) -> Workspace:
    row = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(row)
    await session.flush()
    return row


class ConnectionFactory(Protocol):
    def __call__(
        self,
        workspace: Workspace,
        *,
        connector_type: str = "github",
        name: str = "Test connection",
        auth_type: str = "pat",
        credentials: dict[str, str] | None = None,
        config: dict[str, object] | None = None,
        status: str = "active",
    ) -> Awaitable[Connection]: ...


@pytest.fixture
def make_connection(session: AsyncSession, crypto: SecretCrypto) -> ConnectionFactory:
    """Factory fixture: a stored connection with an encrypted credential."""

    async def factory(
        workspace: Workspace,
        *,
        connector_type: str = "github",
        name: str = "Test connection",
        auth_type: str = "pat",
        credentials: dict[str, str] | None = None,
        config: dict[str, object] | None = None,
        status: str = "active",
    ) -> Connection:
        store = SecretStore(session, crypto)
        secret = await store.create(
            workspace_id=workspace.id,
            name=f"connection:{name}:{new_uuid7().hex[:8]}",
            plaintext=json.dumps(credentials or {"token": "fake-token-value"}),
            secret_type=SecretType.CONNECTION_CREDENTIALS,
        )
        connection = Connection(
            workspace_id=workspace.id,
            connector_type=connector_type,
            name=name,
            auth_type=auth_type,
            status=status,
            encrypted_secret_id=secret.id,
            config_json=config or {},
        )
        session.add(connection)
        await session.flush()
        return connection

    return factory


@pytest.fixture
async def context(
    session: AsyncSession, workspace: Workspace, crypto: SecretCrypto
) -> ToolExecutionContext:
    return ToolExecutionContext(
        session=session,
        workspace_id=workspace.id,
        task_id=new_uuid7(),
        run_id=new_uuid7(),
        agent_id=new_uuid7(),
        agent_name="Scout",
        crypto=crypto,
    )
