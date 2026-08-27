"""Connection lifecycle guards: a disabled connection stays disabled, one MCP
short name per workspace, and a delete that says what it takes with it."""

from collections.abc import Iterator
from typing import TypedDict
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.connections import service
from jhin_api.deps import WorkspaceContext
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_db.models import Connection, Secret, Trigger, TriggerInvocation, User, Workspace
from jhin_domain import ConnectionStatus, TriggerInvocationStatus, WorkspaceRole, new_uuid7
from jhin_secrets import SecretCrypto, SecretStore

MCP_ORIGIN = "https://mcp.example.com"
MCP_URL = f"{MCP_ORIGIN}/mcp"


class _RequestAuditArgs(TypedDict):
    request_id: UUID
    ip_hash: str


REQ: _RequestAuditArgs = {"request_id": new_uuid7(), "ip_hash": "test"}


@pytest.fixture
async def other_admin_ctx(session: AsyncSession) -> WorkspaceContext:
    """A second workspace, to prove the short-name rule is workspace-local."""
    user = User(
        email=f"other-{new_uuid7().hex[:8]}@example.com",
        display_name="Other admin",
        password_hash="x",
    )
    workspace = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add_all([user, workspace])
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=workspace.id, role=WorkspaceRole.ADMIN)


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


async def _github(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    token: str = "fake-github-pat",
    base_url: str = "",
    name: str = "GitHub main",
) -> Connection:
    connection, _ = await service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="github",
        name=name,
        auth_type="pat",
        credentials={"token": token},
        config={"base_url": base_url} if base_url else {},
        **REQ,
    )
    return connection


async def _mcp(
    session: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    name: str,
    slug: str,
) -> Connection:
    connection, _ = await service.create_connection(
        session,
        crypto,
        ctx,
        connector_type="mcp",
        name=name,
        auth_type="bearer",
        credentials={"token": "mcp-token"},
        config={"server_url": MCP_URL, "server_slug": slug},
        **REQ,
    )
    return connection


# --- A disabled connection stays disabled -------------------------------


async def test_verify_reports_health_without_re_enabling_a_disabled_connection(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    """Disabling is how an admin cuts a connection's tools off. A healthy
    verification must not quietly hand them back."""
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)
    await service.set_status(session, admin_ctx, connection.id, disabled=True, **REQ)

    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )

    assert health.ok
    assert updated.status == ConnectionStatus.DISABLED.value
    assert updated.last_verified_at is not None
    assert updated.last_error is None
    # The caller is told the check passed *and* that the app is still off.
    assert "still turned off" in health.message


async def test_verify_of_a_disabled_connection_records_the_real_failure(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection = await _github(
        session, crypto, admin_ctx, token="wrong-token-abc", base_url=fake_github.base_url
    )
    await service.set_status(session, admin_ctx, connection.id, disabled=True, **REQ)

    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )

    assert not health.ok
    assert updated.status == ConnectionStatus.DISABLED.value
    assert updated.last_error is not None
    assert "wrong-token-abc" not in updated.last_error
    # last_error keeps the provider's own words; only the reply carries the note.
    assert "still turned off" not in updated.last_error
    assert "still turned off" in health.message


async def test_verify_still_activates_an_errored_connection(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    """The disabled guard must not touch ordinary recovery."""
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)
    connection.status = ConnectionStatus.ERROR.value
    await session.commit()

    updated, health = await service.verify_connection(
        session, crypto, admin_ctx, connection.id, **REQ
    )

    assert health.ok
    assert updated.status == ConnectionStatus.ACTIVE.value
    assert "still turned off" not in health.message


async def test_rotating_a_credential_leaves_a_disabled_connection_disabled(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)
    await service.set_status(session, admin_ctx, connection.id, disabled=True, **REQ)

    updated = await service.rotate_credentials(
        session, crypto, admin_ctx, connection.id, credentials={"token": "rotated-pat"}, **REQ
    )

    assert updated.status == ConnectionStatus.DISABLED.value
    assert updated.last_verified_at is None
    assert updated.encrypted_secret_id is not None
    store = SecretStore(session, crypto)
    assert "rotated-pat" in await store.reveal(admin_ctx.workspace_id, updated.encrypted_secret_id)


async def test_rotating_a_credential_still_clears_an_error(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)
    connection.status = ConnectionStatus.ERROR.value
    connection.last_error = "bad credential"
    await session.commit()

    updated = await service.rotate_credentials(
        session, crypto, admin_ctx, connection.id, credentials={"token": "rotated-pat"}, **REQ
    )

    assert updated.status == ConnectionStatus.ACTIVE.value
    assert updated.last_error is None


# --- One MCP short name per workspace -----------------------------------


async def test_a_second_connection_cannot_take_an_mcp_short_name(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    """Two connections on one short name means one set of reviewed risk
    levels is silently never enforced."""
    await _mcp(session, crypto, admin_ctx, name="Notion", slug="notion")

    with pytest.raises(HTTPException) as excinfo:
        await _mcp(session, crypto, admin_ctx, name="Notion copy", slug="notion")

    assert excinfo.value.status_code == 409
    assert "notion" in str(excinfo.value.detail)
    assert "Notion" in str(excinfo.value.detail)
    connections = list(
        await session.scalars(
            select(Connection).where(Connection.workspace_id == admin_ctx.workspace_id)
        )
    )
    assert len(connections) == 1
    # A refused create leaves no credential behind.
    secrets = await session.scalar(
        select(func.count())
        .select_from(Secret)
        .where(Secret.workspace_id == admin_ctx.workspace_id)
    )
    assert secrets == 1


async def test_a_free_mcp_short_name_is_accepted(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    await _mcp(session, crypto, admin_ctx, name="Notion", slug="notion")
    second = await _mcp(session, crypto, admin_ctx, name="Linear", slug="linear")

    assert second.config_json["server_slug"] == "linear"


async def test_a_disabled_connection_still_holds_its_mcp_short_name(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
) -> None:
    """Disabling is reversible, so its short name is not up for grabs — the
    tool catalog would resolve the newer connection the moment the older one
    came back."""
    first = await _mcp(session, crypto, admin_ctx, name="Notion", slug="notion")
    await service.set_status(session, admin_ctx, first.id, disabled=True, **REQ)

    with pytest.raises(HTTPException) as excinfo:
        await _mcp(session, crypto, admin_ctx, name="Notion again", slug="notion")

    assert excinfo.value.status_code == 409


async def test_another_workspace_may_use_the_same_mcp_short_name(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    other_admin_ctx: WorkspaceContext,
) -> None:
    await _mcp(session, crypto, admin_ctx, name="Notion", slug="notion")
    elsewhere = await _mcp(session, crypto, other_admin_ctx, name="Notion", slug="notion")

    assert elsewhere.workspace_id == other_admin_ctx.workspace_id


# --- Deleting a connection says what it takes ---------------------------


async def test_access_summary_reports_what_deleting_the_connection_removes(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)
    trigger = Trigger(
        workspace_id=admin_ctx.workspace_id,
        name="On new issue",
        connection_id=connection.id,
        event_type="connector.github.issue.opened",
    )
    session.add(trigger)
    await session.flush()
    session.add_all(
        TriggerInvocation(
            workspace_id=admin_ctx.workspace_id,
            trigger_id=trigger.id,
            idempotency_key=f"key-{index}",
            event_id=new_uuid7(),
            status=TriggerInvocationStatus.STARTED.value,
        )
        for index in range(3)
    )
    await session.commit()

    summary = await service.connection_access_summary(
        session, admin_ctx.workspace_id, connection.id
    )

    assert summary["delete_impact"] == {"trigger_count": 1, "trigger_invocation_count": 3}


async def test_delete_impact_is_zero_when_nothing_depends_on_the_connection(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    fake_github: FakeGitHubServer,
) -> None:
    connection = await _github(session, crypto, admin_ctx, base_url=fake_github.base_url)

    summary = await service.connection_access_summary(
        session, admin_ctx.workspace_id, connection.id
    )

    assert summary["delete_impact"] == {"trigger_count": 0, "trigger_invocation_count": 0}
