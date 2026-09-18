"""Managed app sign-in preserves Jhin ownership and connection identity."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from jhin_api.oauth import service
from jhin_api.oauth.schemas import OAuthStartIn
from jhin_api.settings import Settings
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Connection,
    OAuthAuthorization,
    WorkspaceMembership,
)
from jhin_domain import new_uuid7
from jhin_secrets import SecretStore

REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}
pytestmark = pytest.mark.usefixtures("skip_remote_initial_connection_checks")


class Broker:
    def __init__(self, ctx: Any) -> None:
        self.callback = ""
        self.deleted: list[str] = []
        self.account: dict[str, Any] = {
            "id": "ca_test",
            "status": "ACTIVE",
            "user_id": f"jhin:{ctx.workspace_id}:{ctx.user.id}",
            "toolkit": {"slug": "supabase"},
            "auth_config": {"id": "ac_test", "auth_scheme": "OAUTH2"},
            "state": {"authScheme": "OAUTH2", "val": {"access_token": "provider-secret"}},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-composio-key"
        path = request.url.path
        if request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        if path.endswith("/connected_accounts/complete_auth"):
            return httpx.Response(
                200,
                json={"connected_account_id": self.account["id"], "toolkit_slug": "supabase"},
            )
        if path.endswith("/auth_configs/ac_test"):
            return httpx.Response(
                200,
                json={
                    "toolkit": {"slug": "supabase"},
                    "auth_config": {"id": "ac_test", "auth_scheme": "OAUTH2"},
                },
            )
        if path.endswith("/connected_accounts/link"):
            body = json.loads(request.content)
            self.callback = body["callback_url"]
            assert body["user_id"] == self.account["user_id"]
            return httpx.Response(
                201,
                json={
                    "connected_account_id": self.account["id"],
                    "redirect_url": "https://connect.composio.dev/link/test",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        if path.endswith(f"/connected_accounts/{self.account['id']}"):
            return httpx.Response(200, json=self.account)
        raise AssertionError(path)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_url="https://jhin.example.com",
        composio_api_key="test-composio-key",
        composio_auth_configs={"supabase": "ac_test"},
    )


async def start(session: Any, crypto: Any, ctx: Any, client: Any, **kwargs: Any) -> Any:
    return await service.start_authorization(
        session,
        crypto,
        ctx,
        client,
        settings(),
        OAuthStartIn(
            connector_type="supabase",
            provider_key="composio",
            name="Supabase",
            config={"project_ref": "abcdefghijklmnopqrst"},
            **kwargs,
        ),
        **REQ,
    )


async def finish(session: Any, crypto: Any, ctx: Any, client: Any, broker: Broker) -> Any:
    from jhin_api.oauth.composio import complete

    state = parse_qs(urlsplit(broker.callback).query)["state"][0]
    return await complete(
        session,
        crypto,
        client,
        settings(),
        user_id=ctx.user.id,
        state=state,
        session_uri="test-session-uri",
        **REQ,
    )


@pytest.fixture(autouse=True)
async def member(session: Any, admin_ctx: Any) -> None:
    session.add(
        WorkspaceMembership(
            workspace_id=admin_ctx.workspace_id, user_id=admin_ctx.user.id, role="admin"
        )
    )
    await session.commit()


async def test_supabase_redirects_through_composio_and_stores_only_account_binding(
    session: Any, crypto: Any, admin_ctx: Any
) -> None:
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        result = await start(session, crypto, admin_ctx, client)
        assert result.authorization_url == "https://connect.composio.dev/link/test"
        assert result.client_source == "composio"
        completed = await finish(session, crypto, admin_ctx, client, broker)
        assert completed.error is None
        connection = completed.connection
        assert connection.connector_type == "supabase"
        assert connection.auth_type == "management_token"
        assert connection.oauth_expires_at is None
        assert connection.oauth_issuer == "https://composio.dev"
        assert connection.last_verified_at is not None
        raw = await SecretStore(session, crypto).reveal(
            admin_ctx.workspace_id, connection.encrypted_secret_id
        )
        assert "provider-secret" not in raw
        assert json.loads(raw)["composio_account_id"] == "ca_test"
        again = await finish(session, crypto, admin_ctx, client, broker)
        assert again.public_id == connection.public_id
        assert len((await session.scalars(select(Connection))).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "another-user"},
        {"toolkit": {"slug": "github"}},
        {"status": "INITIATED"},
        {"id": "ca_other"},
        {"auth_config": {"id": "ac_other"}},
    ],
)
async def test_callback_rejects_unverified_account(
    session: Any, crypto: Any, admin_ctx: Any, change: dict[str, Any]
) -> None:
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        broker.account.update(change)
        result = await finish(session, crypto, admin_ctx, client, broker)
    assert result.error is not None
    assert (await session.scalars(select(Connection))).all() == []


async def test_required_project_is_checked_before_hosted_sign_in(
    session: Any, crypto: Any, admin_ctx: Any
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No upstream request expected"))
    ) as client:
        with pytest.raises(HTTPException):
            await service.start_authorization(
                session,
                crypto,
                admin_ctx,
                client,
                settings(),
                OAuthStartIn(connector_type="supabase", provider_key="composio", name="Supabase"),
                **REQ,
            )
    assert (await session.scalars(select(OAuthAuthorization))).all() == []


async def test_reconnect_keeps_id_and_configuration(
    session: Any, crypto: Any, admin_ctx: Any
) -> None:
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        first = await finish(session, crypto, admin_ctx, client, broker)
        await start(session, crypto, admin_ctx, client, connection_id=first.connection.id)
        second = await finish(session, crypto, admin_ctx, client, broker)
        assert second.connection.id == first.connection.id
        assert second.connection.config_json["project_ref"] == "abcdefghijklmnopqrst"


async def test_reconnect_removes_superseded_broker_account_after_success(
    session: Any, crypto: Any, admin_ctx: Any
) -> None:
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        first = await finish(session, crypto, admin_ctx, client, broker)
        connection_id = first.connection.id
        broker.account["id"] = "ca_reconnected"
        await start(session, crypto, admin_ctx, client, connection_id=connection_id)
        second = await finish(session, crypto, admin_ctx, client, broker)
    assert second.error is None
    assert second.connection.id == connection_id
    assert broker.deleted == ["ca_test"]
    raw = await SecretStore(session, crypto).reveal(
        admin_ctx.workspace_id, second.connection.encrypted_secret_id
    )
    assert json.loads(raw)["composio_account_id"] == "ca_reconnected"


async def test_revoked_account_status_survives_error_transaction_rollback(
    session: Any, crypto: Any, admin_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jhin_connectors.composio as managed
    from jhin_api.connections import service as connections

    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        completed = await finish(session, crypto, admin_ctx, client, broker)
    connection_id = completed.connection.id

    async def revoked(*args: Any, **kwargs: Any) -> Any:
        raise managed.ComposioError("Reconnect this app", needs_reauth=True)

    monkeypatch.setattr(managed, "resolve_managed_credentials", revoked)
    with pytest.raises(HTTPException) as caught:
        await connections.verify_connection(session, crypto, admin_ctx, connection_id, **REQ)
    assert caught.value.status_code == 409
    await session.rollback()
    session.expire_all()
    persisted = await session.get(Connection, connection_id)
    assert persisted.status == "needs_reauth"
    assert persisted.last_error == "Reconnect this app"


@pytest.mark.parametrize("succeeds", [True, False], ids=["completed", "rejected-account"])
async def test_browser_sign_in_migrates_local_native_connection_only_after_success(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
    succeeds: bool,
) -> None:
    from jhin_api.connections import service as connections

    workspace_id = admin_ctx.workspace_id
    local_credentials = {"access_token": "existing-local-supabase-token"}
    connection, _ = await connections.create_connection(
        session,
        crypto,
        admin_ctx,
        connector_type="supabase",
        name="Supabase production",
        auth_type="management_token",
        credentials=local_credentials,
        config={"project_ref": "abcdefghijklmnopqrst"},
        **REQ,
    )
    connection_id, public_id = connection.id, connection.public_id
    secret_id, original_status = connection.encrypted_secret_id, connection.status
    original_config = dict(connection.config_json)
    agent = Agent(workspace_id=workspace_id, name="Project assistant", slug="migration-assistant")
    session.add(agent)
    await session.flush()
    scope = {"connection_id": str(connection_id), "project_ref": "abcdefghijklmnopqrst"}
    grant = AgentCapabilityGrant(
        workspace_id=workspace_id,
        agent_id=agent.id,
        capability="supabase.*",
        scope_json=scope,
        effect="allow",
    )
    session.add(grant)
    await session.commit()
    grant_id = grant.id
    store = SecretStore(session, crypto)
    broker = Broker(admin_ctx)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        begun = await start(session, crypto, admin_ctx, client, connection_id=connection_id)
        assert begun.client_source == "composio"
        await session.refresh(connection)
        assert connection.oauth_issuer is None
        assert connection.status == original_status
        assert connection.encrypted_secret_id == secret_id
        assert json.loads(await store.reveal(workspace_id, secret_id)) == local_credentials
        if not succeeds:
            broker.account["status"] = "INITIATED"
        completed = await finish(session, crypto, admin_ctx, client, broker)

    await session.refresh(connection)
    await session.refresh(grant)
    assert connection.id == connection_id and connection.public_id == public_id
    assert connection.name == "Supabase production"
    assert connection.config_json == original_config
    assert connection.auth_type == "management_token"
    assert grant.id == grant_id and grant.scope_json == scope
    assert grant.capability == "supabase.*" and grant.effect == "allow"
    assert len((await session.scalars(select(Connection))).all()) == 1
    assert len((await session.scalars(select(AgentCapabilityGrant))).all()) == 1
    persisted_credentials = json.loads(
        await store.reveal(workspace_id, connection.encrypted_secret_id)
    )
    if succeeds:
        assert completed.error is None and completed.connection.id == connection_id
        assert connection.oauth_issuer == "https://composio.dev"
        assert connection.status == "active"
        assert set(persisted_credentials) == {
            "composio_account_id",
            "composio_user_id",
            "composio_toolkit",
            "composio_auth_config_id",
        }
        assert persisted_credentials["composio_account_id"] == "ca_test"
        assert "provider-secret" not in json.dumps(persisted_credentials)
    else:
        assert completed.error is not None
        assert connection.oauth_issuer is None and connection.status == original_status
        assert connection.encrypted_secret_id == secret_id
        assert persisted_credentials == local_credentials
