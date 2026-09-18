"""A managed app goes from browser-bound sign-in to scoped, approved tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from jhin_api.catalog import service as catalog
from jhin_api.connections import service as connections
from jhin_api.oauth import service as oauth
from jhin_api.oauth.composio import complete
from jhin_api.oauth.schemas import OAuthStartIn
from jhin_api.settings import Settings
from jhin_connectors.composio.source import workspace_composio_tool_definitions
from jhin_db.models import Connection, WorkspaceMembership
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel
from jhin_secrets import SecretStore

REQUEST = {"request_id": new_uuid7(), "ip_hash": "0" * 64}


class ManagedBroker:
    def __init__(self, ctx: Any) -> None:
        self.callback = ""
        self.user_id = f"jhin:{ctx.workspace_id}:{ctx.user.id}"
        self.requests: list[httpx.Request] = []
        self.account: dict[str, Any] = {
            "id": "ca_notion",
            "user_id": f"jhin:{ctx.workspace_id}:{ctx.user.id}",
            "toolkit": {"slug": "notion"},
            "auth_config": {"id": "ac_notion"},
            "status": "ACTIVE",
            "state": {"val": {"access_token": "notion-secret"}},
        }
        self.completion_toolkit = "notion"
        self.tool_version = "20260908_00"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["x-api-key"] == "test-composio-key"
        path = request.url.path
        if path.endswith("/auth_configs/ac_notion"):
            return httpx.Response(
                200,
                json={
                    "toolkit": {"slug": "notion"},
                    "auth_config": {"id": "ac_notion", "auth_scheme": "OAUTH2"},
                },
            )
        if path.endswith("/connected_accounts/link"):
            body = json.loads(request.content)
            assert body["user_id"] == self.account["user_id"]
            self.callback = body["callback_url"]
            return httpx.Response(
                201,
                json={
                    "connected_account_id": "ca_notion",
                    "redirect_url": "https://connect.composio.dev/link/notion",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        if path.endswith("/connected_accounts/complete_auth"):
            assert json.loads(request.content)["user_id"] == self.user_id
            return httpx.Response(
                200,
                json={"connected_account_id": "ca_notion", "toolkit_slug": self.completion_toolkit},
            )
        if path.endswith("/connected_accounts/ca_notion"):
            return httpx.Response(200, json=self.account)
        if path.endswith("/tools"):
            assert request.url.params["toolkit_slug"] == "notion"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "slug": "NOTION_CREATE_PAGE",
                            "name": "Create a page",
                            "description": "Create a page.",
                            "toolkit": {"slug": "notion"},
                            "version": self.tool_version,
                            "input_parameters": {
                                "type": "object",
                                "properties": {"title": {"type": "string"}},
                                "required": ["title"],
                            },
                        }
                    ]
                },
            )
        raise AssertionError(path)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_url="https://jhin.example.com",
        composio_api_key="test-composio-key",
        composio_auth_configs={"notion": "ac_notion"},
    )


async def start(
    session: Any, crypto: Any, ctx: Any, client: httpx.AsyncClient, **kwargs: Any
) -> Any:
    return await oauth.start_authorization(
        session,
        crypto,
        ctx,
        client,
        settings(),
        OAuthStartIn(
            connector_type="composio",
            provider_key="composio",
            name="Notion",
            config={"toolkit": "notion", "server_slug": "notion"},
            **kwargs,
        ),
        **REQUEST,
    )


async def finish(
    session: Any, crypto: Any, ctx: Any, client: httpx.AsyncClient, broker: ManagedBroker
) -> Any:
    state = parse_qs(urlsplit(broker.callback).query)["state"][0]
    return await complete(
        session,
        crypto,
        client,
        settings(),
        user_id=ctx.user.id,
        state=state,
        session_uri="notion-browser-session",
        **REQUEST,
    )


@pytest.fixture(autouse=True)
async def membership(session: Any, admin_ctx: Any) -> None:
    session.add(
        WorkspaceMembership(
            workspace_id=admin_ctx.workspace_id, user_id=admin_ctx.user.id, role="admin"
        )
    )
    await session.commit()


async def test_generic_sign_in_persists_discovery_and_advertises_scoped_tools(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
) -> None:
    broker = ManagedBroker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        begun = await start(session, crypto, admin_ctx, client)
        assert begun.authorization_url == "https://connect.composio.dev/link/notion"
        completed = await finish(session, crypto, admin_ctx, client, broker)
        assert completed.error is None
        connection = completed.connection
        assert connection.connector_type == "composio" and connection.auth_type == "managed"
        assert connection.status == "active" and connection.oauth_issuer == "https://composio.dev"
        assert connection.config_json["toolkit_version"] == "20260908_00"
        assert connection.config_json["composio_tools"][0]["name"] == "NOTION_CREATE_PAGE"
        raw = await SecretStore(session, crypto).reveal(
            admin_ctx.workspace_id, connection.encrypted_secret_id
        )
        assert "notion-secret" not in raw
        assert set(json.loads(raw)) == {
            "composio_account_id",
            "composio_user_id",
            "composio_toolkit",
            "composio_auth_config_id",
        }
        paths = [request.url.path for request in broker.requests]
        assert paths.index("/api/v3.1/connected_accounts/complete_auth") < paths.index(
            "/api/v3.1/tools"
        )
        repeated = await finish(session, crypto, admin_ctx, client, broker)
        assert repeated.public_id == connection.public_id
    # A callback replay rolls back its lookup transaction; the next HTTP
    # request reloads its authenticated user rather than reusing expired ORM state.
    await session.refresh(admin_ctx.user)
    definitions = await workspace_composio_tool_definitions(session, admin_ctx.workspace_id)
    assert [tool.name for tool in definitions] == ["composio.notion.notion_create_page"]
    assert definitions[0].risk is RiskLevel.DESTRUCTIVE
    listing = await connections.list_connection_tools(
        session, crypto, admin_ctx, connection.id, **REQUEST
    )
    assert listing["dynamic"] is True
    assert listing["capability_pattern"] == "composio.notion.*"
    assert listing["tools"][0]["risk"] == "destructive"
    assert set(listing["tools"][0]["scope_keys"]) == {
        "connection_id",
        "server_slug",
        "toolkit",
        "tool",
    }
    overridden = await connections.update_tool_risk_overrides(
        session, admin_ctx, connection.id, overrides={"notion_create_page": "elevated"}, **REQUEST
    )
    assert overridden["tools"][0]["risk"] == "elevated"
    assert (await workspace_composio_tool_definitions(session, admin_ctx.workspace_id))[
        0
    ].risk is RiskLevel.ELEVATED
    with pytest.raises(HTTPException):
        await connections.update_tool_risk_overrides(
            session, admin_ctx, connection.id, overrides={"slack_send_message": "read"}, **REQUEST
        )


@pytest.mark.parametrize(
    "change", ["account_user", "account_toolkit", "inactive", "browser_toolkit", "unpinned_tools"]
)
async def test_generic_callback_refuses_unverified_account_or_discovery(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
    change: str,
) -> None:
    broker = ManagedBroker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        if change == "account_user":
            broker.account["user_id"] = "someone-else"
        elif change == "account_toolkit":
            broker.account["toolkit"] = {"slug": "slack"}
        elif change == "inactive":
            broker.account["status"] = "INITIATED"
        elif change == "browser_toolkit":
            broker.completion_toolkit = "slack"
        else:
            broker.tool_version = "latest"
        completed = await finish(session, crypto, admin_ctx, client, broker)
    assert completed.error is not None
    assert (await session.scalars(select(Connection))).all() == []


async def test_manual_create_cannot_forge_a_managed_connection(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
) -> None:
    with pytest.raises(HTTPException) as error:
        await connections.create_connection(
            session,
            crypto,
            admin_ctx,
            connector_type="composio",
            name="Forged Notion",
            auth_type="managed",
            credentials={},
            config={"toolkit": "notion", "server_slug": "notion"},
            **REQUEST,
        )
    assert error.value.status_code in {400, 422}
    assert (await session.scalars(select(Connection))).all() == []


async def test_duplicate_managed_namespace_is_rejected_before_hosted_sign_in(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
) -> None:
    broker = ManagedBroker(admin_ctx)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker.handle)) as client:
        await start(session, crypto, admin_ctx, client)
        completed = await finish(session, crypto, admin_ctx, client, broker)
        assert completed.error is None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Duplicate namespace must fail before broker requests")
        )
    ) as client:
        with pytest.raises(HTTPException) as error:
            await start(session, crypto, admin_ctx, client)
    assert error.value.status_code == 409


async def test_managed_catalog_app_remains_connectable_without_a_hosted_mcp_endpoint(
    session: Any,
) -> None:
    detail = await catalog.get_entry(session, "airtable")
    assert detail.composio_toolkit == "airtable"
    assert detail.connectable
    assert detail.stdio_only
    assert detail.default_risk == "write"
    assert "toolkit" not in detail.connector_config
    assert detail.config_schema is None


async def test_two_pending_sign_ins_cannot_claim_the_same_managed_namespace(
    session: Any,
    crypto: Any,
    admin_ctx: Any,
) -> None:
    first, second = ManagedBroker(admin_ctx), ManagedBroker(admin_ctx)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(first.handle)) as first_client,
        httpx.AsyncClient(transport=httpx.MockTransport(second.handle)) as second_client,
    ):
        await start(session, crypto, admin_ctx, first_client)
        await start(session, crypto, admin_ctx, second_client)
        winner = await finish(session, crypto, admin_ctx, first_client, first)
        assert winner.error is None
        loser = await finish(session, crypto, admin_ctx, second_client, second)
        assert loser.error is not None
    rows = (await session.scalars(select(Connection))).all()
    assert len(rows) == 1
    assert rows[0].config_json["server_slug"] == "notion"
