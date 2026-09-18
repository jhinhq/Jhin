"""Local browser consent with Supabase-style confidential dynamic registration.

No Composio project, public callback, user API key, or pre-registered OAuth
client. The authorization server is simulated; MCP discovery uses a real
loopback server, and the callback goes through Jhin's public router.
"""

import base64
import hashlib
import json
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from apps.api.tests.oauth_callback_harness import CALLBACK_APP_URL, CallbackHarness
from fastapi import FastAPI
from sqlalchemy import select

from jhin_api.connections import service as connections
from jhin_api.deps import WorkspaceContext, get_oauth_http_client
from jhin_api.oauth import service
from jhin_api.oauth.redirect import CALLBACK_PATH
from jhin_api.oauth.schemas import OAuthProbeIn, OAuthStartIn
from jhin_api.settings import Settings
from jhin_connectors.testing.fake_mcp import FakeMcpServer
from jhin_db.models import Connection, OAuthClientRegistration, Secret
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_secrets import SecretStore

ISSUER = "https://auth.example.com"
CLIENT_SECRET = "generated-by-dynamic-registration"
ACCESS_TOKEN = "issued-after-provider-consent"
REFRESH_TOKEN = "refresh-after-provider-consent"


async def test_localhost_browser_sign_in_without_broker_or_manual_key(
    callback: CallbackHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        app_url=CALLBACK_APP_URL,
        cookie_secure=False,
        composio_api_key="",
        oauth_redirect_base_url="",
    )
    ctx = WorkspaceContext(callback.admin, callback.workspace_id, WorkspaceRole.ADMIN)
    app = cast(FastAPI, callback.client._transport.app)  # type: ignore[attr-defined]
    app.state.settings = settings
    uri = f"{CALLBACK_APP_URL}{CALLBACK_PATH}"
    registrations: list[dict[str, object]] = []
    token_requests: list[dict[str, list[str]]] = []
    authorize_params: dict[str, list[str]] = {}

    with FakeMcpServer(
        require_oauth=True, authorization_server=ISSUER, access_token=ACCESS_TOKEN
    ) as mcp:
        # Only the test resource may use HTTP. No real provider is contacted.
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", mcp.base_url)
        monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "1")

        def authorization_server(request: httpx.Request) -> httpx.Response:
            nonlocal authorize_params
            path = request.url.path
            if path == "/.well-known/oauth-authorization-server":
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "registration_endpoint": f"{ISSUER}/register",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": [
                            "client_secret_basic",
                            "client_secret_post",
                        ],
                        "code_challenge_methods_supported": ["S256"],
                        "authorization_response_iss_parameter_supported": True,
                    },
                )
            if path == "/register":
                document = json.loads(request.content)
                registrations.append(document)
                assert document["redirect_uris"] == [uri]
                assert document["application_type"] == "native"
                assert document["token_endpoint_auth_method"] == "client_secret_basic"
                assert "client_uri" not in document
                return httpx.Response(
                    201,
                    json={
                        "client_id": "automatically-registered-client",
                        "client_secret": CLIENT_SECRET,
                        "token_endpoint_auth_method": "client_secret_basic",
                    },
                )
            if path == "/authorize":
                authorize_params = parse_qs(request.url.query.decode())
                assert authorize_params["redirect_uri"] == [uri]
                assert authorize_params["resource"] == [mcp.resource]
                assert authorize_params["code_challenge_method"] == ["S256"]
                query = urlencode(
                    {
                        "state": authorize_params["state"][0],
                        "code": "approved-local-consent",
                        "iss": ISSUER,
                    }
                )
                return httpx.Response(302, headers={"location": f"{uri}?{query}"})
            if path == "/token":
                form = parse_qs(request.content.decode())
                token_requests.append(form)
                assert form["code"] == ["approved-local-consent"]
                assert form["redirect_uri"] == [uri]
                assert form["resource"] == [mcp.resource]
                digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                assert authorize_params["code_challenge"] == [challenge]
                basic = base64.b64encode(
                    f"automatically-registered-client:{CLIENT_SECRET}".encode()
                ).decode()
                assert request.headers["authorization"] == f"Basic {basic}"
                return httpx.Response(
                    200,
                    json={
                        "access_token": ACCESS_TOKEN,
                        "refresh_token": REFRESH_TOKEN,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "mcp:tools",
                    },
                )
            raise AssertionError(f"Unexpected authorization server request: {path}")

        async with httpx.AsyncClient(
            mounts={ISSUER: httpx.MockTransport(authorization_server)},
            follow_redirects=False,
        ) as oauth_http:
            app.dependency_overrides[get_oauth_http_client] = lambda: oauth_http
            probe = await service.probe(
                callback.session,
                callback.crypto,
                ctx,
                oauth_http,
                settings,
                OAuthProbeIn(connector_type="mcp", server_url=mcp.mcp_url),
            )
            assert probe.method == "oauth_discovery"
            assert probe.supports_dcr
            assert not probe.client_configured
            start = await service.start_authorization(
                callback.session,
                callback.crypto,
                ctx,
                oauth_http,
                settings,
                OAuthStartIn(
                    connector_type="mcp",
                    name="Local browser connection",
                    config={"server_url": mcp.mcp_url, "server_slug": "supabase"},
                ),
                request_id=new_uuid7(),
                ip_hash="",
            )
            assert start.client_source == "dcr"
            assert CLIENT_SECRET not in start.model_dump_json()
            consent = await oauth_http.get(start.authorization_url)
            location = urlsplit(consent.headers["location"])
            response = await callback.client.get(f"{location.path}?{location.query}")
            assert response.status_code == 303
            assert response.headers["location"].startswith(f"{CALLBACK_APP_URL}/apps")
            assert "oauth_error" not in response.headers["location"]

        connection = (await callback.session.scalars(select(Connection))).one()
        assert connection.connector_type == "mcp"
        assert connection.auth_type == "oauth"
        assert connection.oauth_issuer == ISSUER
        assert connection.oauth_resource == mcp.resource
        assert "management_token" not in connection.config_json
        registration = (await callback.session.scalars(select(OAuthClientRegistration))).one()
        assert registration.source == "dcr"
        assert registration.client_secret_id is not None
        assert connection.oauth_client_registration_id == registration.id
        assert connection.oauth_authorized_by_user_id == callback.admin.id
        store = SecretStore(callback.session, callback.crypto)
        assert await store.reveal(ctx.workspace_id, registration.client_secret_id) == CLIENT_SECRET
        assert connection.encrypted_secret_id is not None
        tokens = json.loads(await store.reveal(ctx.workspace_id, connection.encrypted_secret_id))
        assert tokens["access_token"] == ACCESS_TOKEN
        assert tokens["refresh_token"] == REFRESH_TOKEN
        for secret in (await callback.session.scalars(select(Secret))).all():
            assert CLIENT_SECRET.encode() not in secret.ciphertext
            assert ACCESS_TOKEN.encode() not in secret.ciphertext
            assert REFRESH_TOKEN.encode() not in secret.ciphertext

        listing = await connections.list_connection_tools(
            callback.session,
            callback.crypto,
            ctx,
            connection.id,
            request_id=new_uuid7(),
            ip_hash="",
        )
        assert any(tool["name"] == "mcp.supabase.echo" for tool in listing["tools"]), [
            tool["name"] for tool in listing["tools"]
        ]
        assert mcp.oauth is not None
        assert ACCESS_TOKEN in mcp.oauth.presented_tokens
        assert len(registrations) == 1
        assert len(token_requests) == 1
