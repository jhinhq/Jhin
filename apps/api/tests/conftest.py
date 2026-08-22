"""Shared fixtures for API unit tests: in-memory SQLite schema, a workspace
admin context, and envelope crypto — enough to exercise service-layer logic
without the compose stack (integration tests cover the real stack)."""

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_api.deps import WorkspaceContext
from jhin_db.base import Base
from jhin_db.models import User, Workspace
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_observability.bootstrap import _reset_observability_for_test
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


@pytest.fixture(autouse=True)
def restore_process_logging_globals() -> Iterator[None]:
    """Restore root logging and structlog state after every API test.

    Lifespan-owning tests bootstrap the real observability runtime, which
    routes stdlib logging through the JSON handler at the configured level.
    Process-wide logging state must not leak into later test trees: a root
    logger left at ``INFO`` makes SQLAlchemy engines created afterwards echo
    SQL statements and bound parameters into captured logs.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    disabled = root.disabled
    named_state = {
        name: (list(candidate.handlers), candidate.level, candidate.propagate, candidate.disabled)
        for name, candidate in logging.root.manager.loggerDict.items()
        if isinstance(candidate, logging.Logger)
    }
    structlog_config = cast(
        dict[str, Any],
        {
            key: list(value) if key == "processors" else value
            for key, value in structlog.get_config().items()
        },
    )
    try:
        yield
    finally:
        _reset_observability_for_test()
        root.handlers.clear()
        root.handlers.extend(handlers)
        root.setLevel(level)
        root.disabled = disabled
        for name, (named_handlers, named_level, propagate, named_disabled) in named_state.items():
            candidate = logging.root.manager.loggerDict.get(name)
            if not isinstance(candidate, logging.Logger):
                continue
            candidate.handlers.clear()
            candidate.handlers.extend(named_handlers)
            candidate.setLevel(named_level)
            candidate.propagate = propagate
            candidate.disabled = named_disabled
        structlog.configure(**structlog_config)
