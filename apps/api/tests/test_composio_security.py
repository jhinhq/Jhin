"""Security regressions for managed connect and reconnect boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from apps.api.tests.test_composio_oauth import REQ, Broker, finish, settings, start
from fastapi import HTTPException, Response
from sqlalchemy import delete, select

from jhin_api.connections import service as connections
from jhin_api.connections.router import serialize_connection
from jhin_api.oauth import composio, service
from jhin_api.oauth.schemas import OAuthStartIn
from jhin_db.models import Connection, OAuthAuthorization, Workspace, WorkspaceMembership
from jhin_domain import new_uuid7


@pytest.fixture(autouse=True)
async def security_member(session: Any, admin_ctx: Any) -> None:
    session.add(
        WorkspaceMembership(
            workspace_id=admin_ctx.workspace_id, user_id=admin_ctx.user.id, role="admin"
        )
    )
    await session.commit()
    session.expunge(admin_ctx.user)


def state_for(broker: Broker) -> str:
    return parse_qs(urlsplit(broker.callback).query)["state"][0]


async def test_callback_wrong_user_does_not_consume_owner_state(session, crypto, admin_ctx):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        result = await composio.complete(
            session,
            crypto,
            client,
            settings(),
            user_id=new_uuid7(),
            state=state_for(broker),
            session_uri="test-session-uri",
            **REQ,
        )
        assert result.error == "expired"
        row = await session.scalar(select(OAuthAuthorization))
        assert row.consumed_at is None
        assert (await session.scalars(select(Connection))).all() == []


@pytest.mark.parametrize(
    "field,value", [("connected_account_id", "ca_other"), ("toolkit_slug", "github")]
)
async def test_verified_callback_must_match_pending_account_and_toolkit(
    session, crypto, admin_ctx, field, value
):
    broker = Broker(admin_ctx)
    reads = []

    def respond(request):
        if request.url.path.endswith("/connected_accounts/complete_auth"):
            proof = {"connected_account_id": "ca_test", "toolkit_slug": "supabase"}
            proof[field] = value
            return httpx.Response(200, json=proof)
        if request.url.path.endswith("/connected_accounts/ca_test"):
            reads.append(request)
        return broker.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await start(session, crypto, admin_ctx, client)
        result = await finish(session, crypto, admin_ctx, client, broker)
    assert result.error is not None
    assert reads == []
    assert (await session.scalars(select(Connection))).all() == []


async def test_browser_binding_cookie_is_private_and_scoped(session, crypto, admin_ctx):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        result = await start(session, crypto, admin_ctx, client)
    raw = result.model_dump_json()
    assert result._composio_state not in raw
    assert "_composio_state" not in raw
    response = Response()
    composio.set_callback_cookie(response, result, settings())
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert f"Path={composio.CALLBACK_PATH}" in cookie
    assert "Max-Age=600" in cookie


async def test_removed_membership_refuses_callback(session, crypto, admin_ctx):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        await session.execute(
            delete(WorkspaceMembership).where(WorkspaceMembership.user_id == admin_ctx.user.id)
        )
        await session.commit()
        result = await finish(session, crypto, admin_ctx, client, broker)
    assert result.error is not None
    assert (await session.scalars(select(Connection))).all() == []


async def test_expired_state_cannot_create_connection(session, crypto, admin_ctx):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        row = await session.scalar(select(OAuthAuthorization))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        result = await finish(session, crypto, admin_ctx, client, broker)
    assert result.error == "expired"
    assert (await session.scalars(select(Connection))).all() == []


async def test_replayed_success_receipt_requires_current_membership(session, crypto, admin_ctx):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        first = await finish(session, crypto, admin_ctx, client, broker)
        assert first.error is None
        await session.execute(
            delete(WorkspaceMembership).where(WorkspaceMembership.user_id == admin_ctx.user.id)
        )
        await session.commit()
        replay = await finish(session, crypto, admin_ctx, client, broker)
    assert replay.error == "expired"
    assert len((await session.scalars(select(Connection))).all()) == 1


async def test_disabled_reconnect_stays_disabled_and_serialization_hides_binding(
    session, crypto, admin_ctx
):
    broker = Broker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        first = await finish(session, crypto, admin_ctx, client, broker)
        target = first.connection
        target.status = "disabled"
        await session.commit()
        await start(session, crypto, admin_ctx, client, connection_id=target.id)
        second = await finish(session, crypto, admin_ctx, client, broker)
    assert second.error is None
    assert second.connection.id == target.id
    assert second.connection.status == "disabled"
    output = (await serialize_connection(session, second.connection)).model_dump_json()
    for hidden in (
        "provider-secret",
        "test-composio-key",
        "ca_test",
        "ac_test",
        "composio_user_id",
        "encrypted_secret_id",
    ):
        assert hidden not in output


async def test_reconnect_foreign_workspace_target_refused_before_network(
    session, crypto, admin_ctx
):
    foreign = Workspace(name="Foreign", slug=f"foreign-{new_uuid7().hex[:8]}")
    session.add(foreign)
    await session.flush()
    target = Connection(
        workspace_id=foreign.id,
        connector_type="supabase",
        name="Foreign",
        auth_type="management_token",
        config_json={"project_ref": "abcdefghijklmnopqrst"},
    )
    session.add(target)
    await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No upstream call"))
    ) as client:
        with pytest.raises(HTTPException) as error:
            await start(session, crypto, admin_ctx, client, connection_id=target.id)
    assert error.value.status_code == 404


async def test_managed_config_cannot_retarget_an_allowlisted_origin(
    session, crypto, admin_ctx, monkeypatch
):
    target = Connection(
        workspace_id=admin_ctx.workspace_id,
        connector_type="github",
        name="Managed",
        auth_type="oauth",
        oauth_issuer="https://composio.dev",
        config_json={"base_url": "https://api.github.com"},
    )
    session.add(target)
    await session.commit()
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "https://attacker.example")
    with pytest.raises(HTTPException) as error:
        await connections.update_config(
            session, admin_ctx, target.id, config={"base_url": "https://attacker.example"}, **REQ
        )
    assert error.value.status_code in (400, 422)
    assert target.config_json["base_url"] == "https://api.github.com"


async def test_managed_start_returns_safe_refusal_for_custom_origin(
    session, crypto, admin_ctx, monkeypatch
):
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "https://attacker.example")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No upstream call"))
    ) as client:
        with pytest.raises(HTTPException) as error:
            await service.start_authorization(
                session,
                crypto,
                admin_ctx,
                client,
                settings(),
                OAuthStartIn(
                    connector_type="github",
                    provider_key="composio",
                    name="GitHub",
                    config={"base_url": "https://attacker.example"},
                ),
                **REQ,
            )
    assert error.value.status_code in (400, 422)


@pytest.mark.parametrize("mode", ["signed_out", "prefetch"])
async def test_callback_without_session_or_navigation_never_spends_state(
    callback, monkeypatch, mode
):
    async def forbidden(*args, **kwargs):
        pytest.fail("Callback completion must not run")

    monkeypatch.setattr(composio, "complete", forbidden)
    if mode == "signed_out":
        callback.sign_out()
    response = await callback.client.get(
        "/api/v1/oauth/composio/callback?state=some-state",
        headers={"purpose": "prefetch"} if mode == "prefetch" else {},
    )
    assert response.status_code == (204 if mode == "prefetch" else 303)
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""


async def test_callback_ignores_provider_ids_status_and_return_url(callback, monkeypatch):
    received = []

    async def completed(*args, **kwargs):
        received.append(kwargs)
        return service.CallbackResult(connection=None, error="failed")

    monkeypatch.setattr(composio, "complete", completed)
    response = await callback.client.get(
        "/api/v1/oauth/composio/callback",
        headers={"cookie": f"{composio.CALLBACK_COOKIE}=cookie-state"},
        params={
            "state": "safe-state",
            "session_uri": "test-session-uri",
            "connected_account_id": "ca_attacker",
            "status": "ACTIVE",
            "user_id": "attacker",
            "redirect_url": "https://attacker.example",
            "code": "raw-secret",
        },
    )
    assert response.status_code == 303
    assert len(received) == 1
    assert received[0]["state"] == "cookie-state"
    assert received[0]["session_uri"] == "test-session-uri"
    assert received[0]["user_id"] == callback.admin.id
    assert "connected_account_id" not in received[0]
    assert "attacker" not in response.headers["location"]
    assert "raw-secret" not in response.headers["location"]


@pytest.mark.parametrize("missing", ["cookie", "session_uri"])
async def test_callback_cannot_fall_back_to_state_only_polling(callback, monkeypatch, missing):
    async def forbidden(*args, **kwargs):
        pytest.fail("No verified callback context")

    monkeypatch.setattr(composio, "complete", forbidden)
    response = await callback.client.get(
        "/api/v1/oauth/composio/callback",
        headers={}
        if missing == "cookie"
        else {"cookie": f"{composio.CALLBACK_COOKIE}=cookie-state"},
        params={
            "state": "known-state",
            **({"session_uri": "session-secret"} if missing == "cookie" else {}),
        },
    )
    assert response.status_code == 303
    assert "oauth_error=expired" in response.headers["location"]
