"""The whole MCP OAuth path, offline, against two real in-process servers.

A :class:`FakeAuthorizationServer` mints the tokens and a
:class:`FakeMcpServer` in OAuth-protected mode consumes them. Nothing is
mocked: the ``401`` a test sees is a real HTTP response with a real
``WWW-Authenticate``, the token is a real one issued for a real authorization
code, and the tool call goes over the SDK's Streamable HTTP transport with a
real ``Authorization`` header.

What that buys is confidence in the joins rather than in the parts: that the
challenge a server writes reaches discovery, that the audience discovery
records is the one the transport later demands, and that a refused credential
becomes a specific, actionable failure instead of "could not connect".
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_connectors.base import VerifyContext
from jhin_connectors.mcp import McpConnector, McpToolSource
from jhin_connectors.mcp.client import McpAuthChallengeError
from jhin_connectors.mcp.oauth import (
    AUTH_OAUTH,
    OAUTH_ISSUER_KEY,
    OAUTH_PENDING_SCOPE_KEY,
    OAUTH_RESOURCE_KEY,
    OAUTH_SCOPE_KEY,
)
from jhin_connectors.mcp.tools import client_with_headers
from jhin_connectors.registry import build_default_catalog
from jhin_connectors.testing.fake_mcp import FakeMcpServer
from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_db.models import Agent, AgentCapabilityGrant, ToolCall, Workspace
from jhin_domain import ConnectionStatus, ToolCallStatus
from jhin_oauth.discovery import probe_mcp_endpoint, select_scopes
from jhin_oauth.persistence import OAuthClientStore
from jhin_oauth.pkce import generate_pkce, generate_state
from jhin_oauth.registration import register_client
from jhin_oauth.tokens import build_authorization_url, exchange_code
from jhin_oauth.types import AuthorizationServerMetadata, ClientCredentials, TokenResponse
from jhin_secrets import SecretCrypto
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
REDIRECT_URI = "https://jhin.example.com/api/v1/oauth/callback"
SERVER_SLUG = "oauthfake"


@pytest.fixture
def allowlist() -> Iterator[list[str]]:
    """Origins the SSRF policy lets these tests reach, restored afterwards."""
    previous = os.environ.get(ALLOWLIST_ENV)
    origins: list[str] = []

    def apply() -> None:
        os.environ[ALLOWLIST_ENV] = ",".join(origins)

    class _Allowlist(list[str]):
        def append(self, origin: str) -> None:
            super().append(origin)
            origins.append(origin)
            apply()

    try:
        yield _Allowlist()
    finally:
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous


@pytest.fixture
def fake_as(allowlist: list[str]) -> Iterator[FakeAuthorizationServer]:
    with FakeAuthorizationServer(
        FakeAsConfig(scopes_supported=("mcp:tools", "mcp:admin"))
    ) as server:
        allowlist.append(server.base_url)
        yield server


@pytest.fixture
def oauth_mcp(fake_as: FakeAuthorizationServer, allowlist: list[str]) -> Iterator[FakeMcpServer]:
    with FakeMcpServer(
        require_oauth=True,
        authorization_server=fake_as.issuer,
        scopes=("mcp:tools",),
        additional_scopes=("mcp:admin",),
    ) as server:
        allowlist.append(server.base_url)
        yield server


@pytest.fixture
def root_prm_mcp(fake_as: FakeAuthorizationServer, allowlist: list[str]) -> Iterator[FakeMcpServer]:
    """A server that publishes only at the origin root and does not name its
    metadata document in the challenge — the shape that forces the client onto
    the constructed well-known candidates."""
    with FakeMcpServer(
        require_oauth=True,
        authorization_server=fake_as.issuer,
        prm_style="root",
        advertise_resource_metadata=False,
    ) as server:
        allowlist.append(server.base_url)
        yield server


async def _authorize(
    fake_as: FakeAuthorizationServer, mcp: FakeMcpServer
) -> tuple[AuthorizationServerMetadata, ClientCredentials, TokenResponse]:
    """Run one real authorization: probe, register, consent, exchange."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        probe = await probe_mcp_endpoint(client, mcp.mcp_url)
        assert probe.authorization_server is not None
        assert probe.protected_resource is not None
        metadata = probe.authorization_server
        credentials = await register_client(
            client, metadata, redirect_uri=REDIRECT_URI, client_name="Jhin"
        )
        scope = select_scopes(
            challenge_scope=probe.challenge_scope,
            resource_scopes=probe.protected_resource.scopes_supported,
            server_scopes=metadata.scopes_supported,
            want_offline_access=True,
        )
        pkce = generate_pkce()
        url = build_authorization_url(
            metadata,
            client_id=credentials.client_id,
            redirect_uri=REDIRECT_URI,
            state=generate_state(),
            pkce=pkce,
            scope=scope,
            resource=probe.protected_resource.resource,
        )
        location = fake_as.authorize(url)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code"][0]
        tokens = await exchange_code(
            client,
            metadata,
            credentials=credentials,
            code=code,
            redirect_uri=REDIRECT_URI,
            code_verifier=pkce.verifier,
            resource=probe.protected_resource.resource,
        )
    assert mcp.oauth is not None
    mcp.oauth.accept_only(tokens.access_token)
    return metadata, credentials, tokens


def _oauth_config(mcp: FakeMcpServer, tokens: TokenResponse, **extra: Any) -> dict[str, Any]:
    return {
        "server_url": mcp.mcp_url,
        "server_slug": SERVER_SLUG,
        "transport": "auto",
        OAUTH_RESOURCE_KEY: mcp.resource,
        OAUTH_ISSUER_KEY: tokens.issuer,
        OAUTH_SCOPE_KEY: tokens.scope,
        **extra,
    }


def _oauth_ctx(mcp: FakeMcpServer, tokens: TokenResponse, **extra: Any) -> VerifyContext:
    return VerifyContext(
        auth_type=AUTH_OAUTH,
        credentials={"access_token": tokens.access_token, "token_type": tokens.token_type},
        config=_oauth_config(mcp, tokens, **extra),
    )


# --- discovery --------------------------------------------------------------


async def test_the_401_challenge_leads_all_the_way_to_the_authorization_server(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        probe = await probe_mcp_endpoint(client, oauth_mcp.mcp_url)

    assert probe.requires_auth is True
    assert probe.failure_reason is None
    assert probe.resource_metadata_url == oauth_mcp.prm_url
    assert probe.challenge_scope == "mcp:tools"
    assert probe.protected_resource is not None
    assert probe.protected_resource.resource == oauth_mcp.resource
    assert probe.authorization_server is not None
    assert probe.authorization_server.issuer == fake_as.issuer
    assert probe.supports_oauth is True
    assert probe.supports_dcr is True


async def test_a_server_that_names_no_metadata_url_is_still_discovered(
    fake_as: FakeAuthorizationServer, root_prm_mcp: FakeMcpServer
) -> None:
    """RFC 9728's constructed candidates, exercised: no ``resource_metadata``
    in the challenge and nothing published at the path-inserted URL."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        probe = await probe_mcp_endpoint(client, root_prm_mcp.mcp_url)

    assert probe.requires_auth is True
    assert probe.resource_metadata_url is None
    assert probe.protected_resource is not None
    assert probe.protected_resource.source_url.endswith("/.well-known/oauth-protected-resource")
    assert probe.authorization_server is not None
    assert probe.authorization_server.issuer == fake_as.issuer


# --- the connector, once a token exists -------------------------------------


async def test_an_authorized_connection_verifies_and_discovers_tools(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert tokens.access_token
    assert tokens.issuer == fake_as.issuer

    health = await McpConnector().verify_connection(_oauth_ctx(oauth_mcp, tokens))
    assert health.ok, health.message
    assert health.message.startswith("ok: 6 tools")
    assert health.details["server_name"] == "Fake MCP"

    discovery = await McpConnector().refresh_discovery(_oauth_ctx(oauth_mcp, tokens))
    assert discovery is not None
    assert oauth_mcp.oauth is not None
    assert oauth_mcp.oauth.authorized_calls > 0
    assert oauth_mcp.oauth.presented_tokens[-1] == tokens.access_token


async def test_a_revoked_token_asks_for_a_reconnect_and_leaks_nothing(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.reject_all()

    health = await McpConnector().verify_connection(_oauth_ctx(oauth_mcp, tokens))
    assert health.ok is False
    assert health.details["needs_reauth"] == "true"
    assert "reconnect it" in health.message
    assert "401" in health.message
    assert tokens.access_token not in health.message
    assert oauth_mcp.mcp_url not in health.message
    # The server's own error_description never crosses the boundary.
    assert "expired or revoked" not in health.message


async def test_a_grant_that_is_too_narrow_reads_as_a_step_up_not_a_reconnect(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.require_scope("mcp:admin")

    health = await McpConnector().verify_connection(_oauth_ctx(oauth_mcp, tokens))
    assert health.ok is False
    assert health.details["needs_reauth"] == "true"
    assert "extra permission" in health.message
    assert "403" in health.message


async def test_a_token_is_not_sent_to_a_server_the_connection_was_repointed_at(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    """Editing ``server_url`` after authorization must not silently re-aim the
    credential; the refusal happens before anything is dialled."""
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert oauth_mcp.oauth is not None
    before = oauth_mcp.oauth.snapshot()

    ctx = VerifyContext(
        auth_type=AUTH_OAUTH,
        credentials={"access_token": tokens.access_token},
        config=_oauth_config(
            oauth_mcp, tokens, **{OAUTH_RESOURCE_KEY: "https://elsewhere.example"}
        ),
    )
    health = await McpConnector().verify_connection(ctx)
    assert health.ok is False
    assert "no longer matches the account it was authorized for" in health.message
    assert oauth_mcp.oauth.snapshot() == before


async def test_the_retry_transport_dials_again_with_a_fresh_token(
    fake_as: FakeAuthorizationServer, oauth_mcp: FakeMcpServer
) -> None:
    """The mechanical half of refresh-then-retry: the same session body, run
    against the same server, succeeds once the header carries a token the
    server accepts."""
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert oauth_mcp.oauth is not None
    config = _oauth_config(oauth_mcp, tokens)

    async def body(session: Any) -> int:
        listed = await session.list_tools()
        return len(listed.tools)

    stale = client_with_headers(config, headers={"Authorization": "Bearer stale-token-value"})
    with pytest.raises(McpAuthChallengeError) as caught:
        await stale.run(body)
    assert caught.value.status_code == 401
    assert "resource_metadata" in caught.value.www_authenticate

    oauth_mcp.oauth.accept("rotated-access-token-value")
    fresh = client_with_headers(
        config, headers={"Authorization": "Bearer rotated-access-token-value"}
    )
    assert await fresh.run(body) == 6


# --- through the gateway ----------------------------------------------------


async def _grant(
    session: AsyncSession, context: ToolExecutionContext, capability: str, scope: dict[str, str]
) -> None:
    if await session.scalar(select(Agent).where(Agent.id == context.agent_id)) is None:
        session.add(
            Agent(
                id=context.agent_id,
                workspace_id=context.workspace_id,
                name=context.agent_name,
                slug=f"scout-{context.agent_id.hex[:6]}",
            )
        )
    session.add(
        AgentCapabilityGrant(
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            capability=capability,
            scope_json=scope,
            effect="allow",
        )
    )
    await session.flush()


async def _workspace_catalog(context: ToolExecutionContext) -> ToolCatalog:
    catalog = build_default_catalog()
    return await catalog.for_workspace(context.session, context.workspace_id)


async def _oauth_connection(
    workspace: Workspace,
    make_connection: Any,
    mcp: FakeMcpServer,
    tokens: TokenResponse,
    *,
    refresh_token: str | None = None,
) -> Any:
    discovery = await McpConnector().refresh_discovery(_oauth_ctx(mcp, tokens))
    assert discovery is not None
    credentials = {"access_token": tokens.access_token, "token_type": tokens.token_type}
    if refresh_token is not None:
        credentials["refresh_token"] = refresh_token
    return await make_connection(
        workspace,
        connector_type="mcp",
        name="Fake OAuth MCP",
        auth_type=AUTH_OAUTH,
        credentials=credentials,
        config={**_oauth_config(mcp, tokens), **discovery},
    )


async def test_a_granted_tool_runs_with_the_oauth_bearer_token(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_as: FakeAuthorizationServer,
    oauth_mcp: FakeMcpServer,
) -> None:
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    connection = await _oauth_connection(workspace, make_connection, oauth_mcp, tokens)
    await _grant(session, context, f"mcp.{SERVER_SLUG}.*", {"connection_id": str(connection.id)})
    gateway = ToolGateway(context, await _workspace_catalog(context))

    outcome = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "executed", outcome
    assert outcome.sanitized_output["text"] == "hi"
    row = await session.scalar(select(ToolCall).where(ToolCall.id == outcome.tool_call_id))
    assert row is not None and row.status == ToolCallStatus.COMPLETED.value
    assert tokens.access_token not in str(row.sanitized_output_json)


async def test_a_rejected_token_with_nothing_to_refresh_asks_for_a_reconnect(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_as: FakeAuthorizationServer,
    oauth_mcp: FakeMcpServer,
) -> None:
    """No refresh token means no exchange to attempt: the agent is told the
    one thing that will fix it, and the authorization server is not troubled
    for an answer already known."""
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    connection = await _oauth_connection(workspace, make_connection, oauth_mcp, tokens)
    await _grant(session, context, f"mcp.{SERVER_SLUG}.*", {"connection_id": str(connection.id)})
    gateway = ToolGateway(context, await _workspace_catalog(context))
    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.reject_all()
    challenges_before = oauth_mcp.oauth.snapshot()["challenges"]

    outcome = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "failed", outcome
    assert outcome.error_code == "mcp_oauth_reauth_required"
    hint = str((outcome.sanitized_output or {}).get("hint"))
    assert "reconnect" in hint.lower()
    assert connection.name in hint
    assert tokens.access_token not in hint
    row = await session.scalar(select(ToolCall).where(ToolCall.id == outcome.tool_call_id))
    assert row is not None
    assert row.error_code == "mcp_oauth_reauth_required"
    assert tokens.access_token not in str(row.sanitized_output_json)
    # Refused once and not dialled again: with no refresh token there is
    # nothing to exchange, so no retry is attempted.
    assert oauth_mcp.oauth.snapshot()["challenges"] == challenges_before + 1


async def test_an_insufficient_scope_parks_the_wider_scope_and_asks_once(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    fake_as: FakeAuthorizationServer,
    oauth_mcp: FakeMcpServer,
) -> None:
    _metadata, _credentials, tokens = await _authorize(fake_as, oauth_mcp)
    connection = await _oauth_connection(workspace, make_connection, oauth_mcp, tokens)
    await _grant(session, context, f"mcp.{SERVER_SLUG}.*", {"connection_id": str(connection.id)})
    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.require_scope("mcp:admin")

    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "failed", outcome
    assert outcome.error_code == "mcp_oauth_scope_required"
    hint = str((outcome.sanitized_output or {}).get("hint"))
    assert "additional permission (1 new scopes)" in hint
    assert "mcp:admin" not in hint

    # The step-up rides the same transaction as the tool-call record, which
    # the caller commits once the gateway returns.
    await session.commit()
    await session.refresh(connection)
    assert connection.status == ConnectionStatus.NEEDS_REAUTH.value
    pending = connection.config_json[OAUTH_PENDING_SCOPE_KEY]
    assert "mcp:admin" in pending.split()
    assert "mcp:tools" in pending.split()

    # Asked once. The second refusal for the same tool inside the cooldown is
    # a permanent failure rather than another reconnect request.
    connection.status = ConnectionStatus.ACTIVE.value
    await session.commit()
    gateway = ToolGateway(context, await _workspace_catalog(context))
    repeat = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert repeat.status == "failed"
    repeat_hint = str((repeat.sanitized_output or {}).get("hint"))
    assert "still refuses this tool" in repeat_hint
    await session.commit()
    await session.refresh(connection)
    assert connection.status == ConnectionStatus.ACTIVE.value


async def test_the_static_auth_schemes_are_untouched_by_any_of_this(
    session: AsyncSession, workspace: Workspace
) -> None:
    """A workspace with no OAuth connection still loads its MCP tools exactly
    as before; nothing in the OAuth path runs for a bearer connection."""
    assert await McpToolSource().load(session, workspace.id) == []


async def test_a_rejected_token_is_refreshed_once_and_the_call_retried(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    crypto: SecretCrypto,
    fake_as: FakeAuthorizationServer,
    oauth_mcp: FakeMcpServer,
) -> None:
    """The #1 way OAuth access is lost in the field: a token the client still
    believes in that the resource has already stopped honouring. One forced
    exchange and one retry, and the agent never learns anything went wrong."""
    metadata, credentials, tokens = await _authorize(fake_as, oauth_mcp)
    assert tokens.refresh_token
    connection = await _oauth_connection(
        workspace, make_connection, oauth_mcp, tokens, refresh_token=tokens.refresh_token
    )
    registration = await OAuthClientStore(session, crypto).save(
        workspace_id=workspace.id,
        issuer=metadata.issuer,
        redirect_uri=REDIRECT_URI,
        credentials=credentials,
        scopes=tokens.scope,
        source="dcr",
        created_by_user_id=None,
    )
    connection.oauth_client_registration_id = registration.id
    connection.oauth_issuer = metadata.issuer
    connection.oauth_resource = oauth_mcp.resource
    connection.oauth_scope = tokens.scope
    connection.config_json = {
        **connection.config_json,
        "oauth_token_endpoint": metadata.token_endpoint,
    }
    await session.commit()
    await _grant(session, context, f"mcp.{SERVER_SLUG}.*", {"connection_id": str(connection.id)})

    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.revoke(tokens.access_token)

    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "executed", outcome
    assert outcome.sanitized_output["text"] == "hi"

    presented = oauth_mcp.oauth.presented_tokens[-1]
    assert presented != tokens.access_token
    await session.commit()
    await session.refresh(connection)
    assert connection.status == ConnectionStatus.ACTIVE.value
    assert connection.oauth_refresh_failures == 0


async def test_the_retry_happens_at_most_once(
    session: AsyncSession,
    workspace: Workspace,
    make_connection: Any,
    context: ToolExecutionContext,
    crypto: SecretCrypto,
    fake_as: FakeAuthorizationServer,
    oauth_mcp: FakeMcpServer,
) -> None:
    """A server refusing a token minted seconds ago is not a race, and asking
    the authorization server again would only burn refresh tokens."""
    metadata, credentials, tokens = await _authorize(fake_as, oauth_mcp)
    connection = await _oauth_connection(
        workspace, make_connection, oauth_mcp, tokens, refresh_token=tokens.refresh_token
    )
    registration = await OAuthClientStore(session, crypto).save(
        workspace_id=workspace.id,
        issuer=metadata.issuer,
        redirect_uri=REDIRECT_URI,
        credentials=credentials,
        scopes=tokens.scope,
        source="dcr",
        created_by_user_id=None,
    )
    connection.oauth_client_registration_id = registration.id
    connection.oauth_issuer = metadata.issuer
    connection.oauth_resource = oauth_mcp.resource
    connection.oauth_scope = tokens.scope
    connection.config_json = {
        **connection.config_json,
        "oauth_token_endpoint": metadata.token_endpoint,
    }
    await session.commit()
    await _grant(session, context, f"mcp.{SERVER_SLUG}.*", {"connection_id": str(connection.id)})

    assert oauth_mcp.oauth is not None
    oauth_mcp.oauth.reject_all()
    challenges_before = oauth_mcp.oauth.snapshot()["challenges"]

    gateway = ToolGateway(context, await _workspace_catalog(context))
    outcome = await gateway.request(
        f"mcp.{SERVER_SLUG}.echo",
        f'{{"connection_id": "{connection.id}", "arguments": {{"text": "hi"}}}}',
    )
    assert outcome.status == "failed", outcome
    assert outcome.error_code == "mcp_oauth_reauth_required"
    hint = str((outcome.sanitized_output or {}).get("hint"))
    assert "reconnect" in hint.lower()
    # Refused, refreshed, refused again — and then stopped.
    assert oauth_mcp.oauth.snapshot()["challenges"] == challenges_before + 2
