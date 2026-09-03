"""Shared fixtures for API unit tests: in-memory SQLite schema, a workspace
admin context, envelope crypto, and a harness around the public OAuth
callbacks — enough to exercise service-layer logic without the compose stack
(integration tests cover the real stack)."""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import structlog
from apps.api.tests.oauth_callback_harness import (
    CALLBACK_APP_URL,
    CallbackHarness,
)
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_api.deps import (
    AuthContext,
    Principal,
    WorkspaceContext,
    get_current_auth,
    get_current_auth_optional,
    get_current_principal,
    get_db,
)
from jhin_api.oauth.router import oauth_public_router
from jhin_api.settings import Settings
from jhin_db.base import Base
from jhin_db.models import User, UserSession, Workspace, WorkspaceMembership
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


@pytest.fixture
async def callback(
    session: AsyncSession,
    crypto: SecretCrypto,
) -> AsyncIterator[CallbackHarness]:
    admin = User(
        email=f"oauth-admin-{new_uuid7().hex[:8]}@example.com",
        display_name="OAuth Admin",
        password_hash="x",
    )
    other = User(
        email=f"oauth-other-{new_uuid7().hex[:8]}@example.com",
        display_name="Someone Else",
        password_hash="x",
    )
    workspace = Workspace(name="OAuth", slug=f"oauth-{new_uuid7().hex[:8]}")
    session.add_all([admin, other, workspace])
    await session.flush()
    session.add_all(
        [
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=admin.id, role=WorkspaceRole.ADMIN.value
            ),
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=other.id, role=WorkspaceRole.ADMIN.value
            ),
        ]
    )
    await session.commit()

    actor = {"user": admin}
    app = FastAPI()
    app.state.settings = Settings(_env_file=None, app_url=CALLBACK_APP_URL)
    app.state.secret_crypto = crypto

    class _NoTemporal:
        async def get(self) -> Any:
            raise RuntimeError("temporal is unavailable in this test")

    app.state.temporal_provider = _NoTemporal()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(oauth_public_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"oauth-callback-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    async def override_principal() -> Principal:
        return Principal(user=actor["user"])

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_auth] = override_auth
    # The callbacks resolve their session through the optional variant so an
    # expired one becomes a redirect rather than a raw 401 body. It is a
    # separate callable, so it needs its own override here; ``sign_out`` is
    # how a test makes it deliberately return None.
    app.dependency_overrides[get_current_auth_optional] = override_auth
    app.dependency_overrides[get_current_principal] = override_principal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield CallbackHarness(
            client=client,
            session=session,
            crypto=crypto,
            workspace_id=workspace.id,
            actor=actor,
            admin=admin,
            other=other,
        )


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
