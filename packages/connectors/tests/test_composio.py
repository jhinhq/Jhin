"""Managed auth binding and the broker's bounded HTTP boundary."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from jhin_connectors.composio import (
    COMPOSIO_ISSUER,
    ComposioClient,
    ComposioError,
    managed_user_id,
    resolve_managed_credentials,
    validate_native_target,
)


def managed(connector="github"):
    workspace, user = uuid4(), uuid4()
    connection = SimpleNamespace(
        connector_type=connector,
        oauth_issuer=COMPOSIO_ISSUER,
        workspace_id=workspace,
        oauth_authorized_by_user_id=user,
        auth_type={
            "github": "oauth",
            "linear": "oauth",
            "vercel": "access_token",
            "supabase": "management_token",
        }[connector],
        config_json={},
    )
    binding = {
        "composio_account_id": "ca_test",
        "composio_user_id": managed_user_id(workspace, user),
        "composio_toolkit": connector,
        "composio_auth_config_id": "ac_test",
    }
    account = {
        "id": "ca_test",
        "user_id": binding["composio_user_id"],
        "toolkit": {"slug": connector},
        "auth_config": {"id": "ac_test"},
        "status": "ACTIVE",
        "state": {"val": {"access_token": "provider-secret-token"}},
    }
    return connection, binding, account


@pytest.mark.parametrize(
    "connector,key",
    [
        ("github", "access_token"),
        ("linear", "access_token"),
        ("vercel", "token"),
        ("supabase", "access_token"),
    ],
)
async def test_native_credential_mapping(connector, key):
    connection, binding, account = managed(connector)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=account))
    ) as http:
        result = await resolve_managed_credentials(
            connection, binding, client=ComposioClient(http, api_key="broker-secret")
        )
    assert result == {key: "provider-secret-token"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "ca_other"),
        ("user_id", "someone-else"),
        ("toolkit", {"slug": "slack"}),
        ("auth_config", {"id": "ac_other"}),
        ("status", "EXPIRED"),
        ("is_disabled", True),
        ("auth_config", {"id": "ac_test", "is_disabled": True}),
    ],
)
async def test_account_binding_fails_closed(field, value):
    connection, binding, account = managed()
    account[field] = value
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=account))
    ) as http:
        with pytest.raises(ComposioError) as error:
            await resolve_managed_credentials(
                connection, binding, client=ComposioClient(http, api_key="broker-secret")
            )
    assert "provider-secret" not in str(error.value)
    assert "broker-secret" not in str(error.value)


async def test_modified_provider_origin_refused_before_credentials_are_fetched():
    connection, binding, _ = managed()
    connection.config_json = {"base_url": "https://example.com"}

    def forbidden(request):
        pytest.fail("must refuse before contacting broker")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(ComposioError, match="origin"):
            await resolve_managed_credentials(
                connection, binding, client=ComposioClient(http, api_key="broker-secret")
            )


async def test_local_credentials_unchanged():
    connection, _, _ = managed()
    connection.oauth_issuer = None
    credentials = {"token": "local"}
    assert await resolve_managed_credentials(connection, credentials) is credentials


@pytest.mark.parametrize("change", ["user", "toolkit"])
async def test_community_binding_must_match_local_owner_before_api_verification(change):
    connection, binding, _ = managed()
    connection.connector_type = "composio"
    connection.auth_type = "managed"
    connection.config_json = {"toolkit": "github"}
    binding["composio_user_id" if change == "user" else "composio_toolkit"] = "wrong"
    with pytest.raises(ComposioError):
        await resolve_managed_credentials(connection, binding)


async def test_link_response_parses_expiry_and_uses_link_endpoint():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "connected_account_id": "ca_test",
                "redirect_url": "https://connect.composio.dev/link/test",
                "expires_at": "2026-09-08T20:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        link = await ComposioClient(http, api_key="broker-secret").create_link(
            "ac_test", "jhin:user", "https://jhin.example/callback"
        )
    assert isinstance(link.expires_at, datetime)
    assert link.expires_at.tzinfo is not None
    assert seen[0].url.path == "/api/v3.1/connected_accounts/link"


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com",
        "https://api.github.com.evil.com",
        "https://api.github.com/path",
        "https://x@api.github.com",
        "http://api.github.com",
    ],
)
def test_native_target_is_pinned(target):
    with pytest.raises(ComposioError):
        validate_native_target("github", "oauth", {"base_url": target})


async def test_oversized_broker_response_rejected():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 524_289))
    ) as http:
        with pytest.raises(ComposioError):
            await ComposioClient(http, api_key="broker-secret").get_account("ca_test")


async def test_auth_config_envelope_preserves_toolkit():
    envelope = {
        "toolkit": {"slug": "github"},
        "auth_config": {"id": "ac_test", "auth_scheme": "OAUTH2"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))
    ) as http:
        client = ComposioClient(http, api_key="broker-secret")
        assert await client.create_auth_config("github") == envelope
        assert await client.get_auth_config("ac_test") == envelope


async def test_ensure_auth_config_reuses_active_managed_oauth():
    seen = []
    config = {
        "id": "ac_test",
        "toolkit": {"slug": "github"},
        "auth_scheme": "OAUTH2",
        "is_composio_managed": True,
        "status": "ENABLED",
    }

    def respond(request):
        seen.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/auth_configs"):
            assert request.url.params["toolkit_slug"] == "github"
            assert request.url.params["show_disabled"] == "false"
            return httpx.Response(200, json={"items": [config]})
        return httpx.Response(200, json=config)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await ComposioClient(http, api_key="broker-secret").ensure_auth_config("github")
    assert result["id"] == "ac_test"
    assert len(seen) == 2


async def test_ensure_auth_config_creates_when_no_usable_config():
    methods = []

    def respond(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "ac_bad",
                            "status": "DISABLED",
                            "auth_scheme": "OAUTH2",
                            "is_composio_managed": True,
                            "toolkit": {"slug": "github"},
                        }
                    ]
                },
            )
        return httpx.Response(
            201, json={"toolkit": {"slug": "github"}, "auth_config": {"id": "ac_new"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await ComposioClient(http, api_key="broker-secret").ensure_auth_config("github")
    assert result["auth_config"]["id"] == "ac_new"
    assert methods == ["GET", "POST"]


async def test_legacy_account_data_token_shape():
    connection, binding, account = managed()
    account["data"] = account.pop("state")["val"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=account))
    ) as http:
        result = await resolve_managed_credentials(
            connection, binding, client=ComposioClient(http, api_key="broker-secret")
        )
    assert result == {"access_token": "provider-secret-token"}


async def test_invalid_key_transport_encoding_is_safe():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No request"))
    ) as http:
        with pytest.raises(ComposioError) as error:
            await ComposioClient(http, api_key="secret-🔐").get_account("ca_test")
    assert "secret" not in str(error.value)


async def test_complete_auth_posts_opaque_session_only_to_broker():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"connected_account_id": "ca_test", "toolkit_slug": "github"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await ComposioClient(http, api_key="broker-secret").complete_auth(
            session_uri="https://opaque.invalid/session-secret", user_id="jhin:workspace:user"
        )
    assert result == {"connected_account_id": "ca_test", "toolkit_slug": "github"}
    assert len(seen) == 1
    assert (
        str(seen[0].url) == "https://backend.composio.dev/api/v3.1/connected_accounts/complete_auth"
    )
    assert seen[0].method == "POST"
    from jhin_secrets.redaction import get_redactor

    assert "session-secret" not in get_redactor().redact_text(
        "https://opaque.invalid/session-secret"
    )


@pytest.mark.parametrize("status", [302, 401, 500])
async def test_http_failures_never_echo_provider_or_follow_redirects(status):
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://example.com"},
            json={"error": "secret-provider-response"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response), follow_redirects=True
    ) as http:
        with pytest.raises(ComposioError) as error:
            await ComposioClient(http, api_key="broker-secret").get_account("ca_test")
    assert len(seen) == 1
    assert str(seen[0].url) == "https://backend.composio.dev/api/v3.1/connected_accounts/ca_test"
    assert "secret" not in str(error.value)


async def test_execution_resolver_uses_managed_credentials(
    workspace, context, make_connection, monkeypatch
):
    from jhin_connectors.execution import resolve_connection

    connection = await make_connection(
        workspace, auth_type="oauth", credentials={"composio_account_id": "ca_test"}
    )
    connection.oauth_issuer = COMPOSIO_ISSUER

    async def resolved(row, credentials, **kwargs):
        assert row.id == connection.id
        assert credentials == {"composio_account_id": "ca_test"}
        return {"access_token": "native-token"}

    monkeypatch.setattr("jhin_connectors.composio.resolve_managed_credentials", resolved)
    result = await resolve_connection(context, connection.id, connector_type="github")
    assert result.credentials == {"access_token": "native-token"}
