"""GitHub's device flow, end to end against the in-process fake.

The property under test is the one that makes this flow worth having: a
client id and nothing else. No client secret at the code request, at any
poll, or at the refresh; no redirect URI anywhere; and every error GitHub
answers with HTTP 200 handled as the refusal it is. The fake records every
request body, so "no secret was sent" is asserted on the wire, not inferred.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from jhin_connectors.base import VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.github.auth import AUTH_DEVICE
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.oauth import (
    GITHUB_API_ORIGIN,
    device_flow_message,
    poll_github_device_token,
    start_github_device_authorization,
)
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_connectors.testing.fake_github_oauth import (
    FakeGitHubOAuthConfig,
    FakeGitHubOAuthServer,
)
from jhin_oauth.errors import DeviceAuthorizationDenied, DeviceCodeExpired, TokenError
from jhin_oauth.tokens import next_poll_interval, refresh_access_token
from jhin_oauth.types import (
    AuthorizationServerMetadata,
    ClientCredentials,
    DeviceTokenPending,
    TokenResponse,
)

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
CLIENT_ID = "Iv23liFakeClientId"
# The one sentence the fake puts in every error_description. It must never
# reach a message Jhin raises.
PROVIDER_PROSE = "fake GitHub says"


@pytest.fixture(autouse=True)
def _restore_allowlist() -> Iterator[None]:
    """Every test allow-lists its fake's origin and puts the env back after."""
    previous = os.environ.get(ALLOWLIST_ENV)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous


def _allow(*origins: str) -> None:
    current = [entry for entry in os.environ.get(ALLOWLIST_ENV, "").split(",") if entry]
    os.environ[ALLOWLIST_ENV] = ",".join([*current, *origins])


@contextmanager
def _fake_github(config: FakeGitHubOAuthConfig | None = None) -> Iterator[FakeGitHubOAuthServer]:
    with FakeGitHubOAuthServer(config) as server:
        _allow(server.base_url)
        yield server


def _metadata(server: FakeGitHubOAuthServer) -> AuthorizationServerMetadata:
    """GitHub publishes no RFC 8414 document, so refresh needs one built here.

    Constructed directly because there is nothing to parse; the token endpoint
    is re-validated against outbound policy on every call regardless.
    """
    return AuthorizationServerMetadata(
        issuer=server.base_url,
        authorization_endpoint=server.authorize_url,
        token_endpoint=server.device_token_url,
        device_authorization_endpoint=server.device_code_url,
        code_challenge_methods_supported=("S256",),
        grant_types_supported=("authorization_code", "refresh_token"),
        token_endpoint_auth_methods_supported=("none",),
    )


def _every_body(server: FakeGitHubOAuthServer) -> list[dict[str, str]]:
    device_code_bodies = [dict(record["form"]) for record in server.device_code_requests]
    return device_code_bodies + server.recorded_bodies()


async def test_device_flow_completes_without_a_client_secret_anywhere() -> None:
    with _fake_github(
        FakeGitHubOAuthConfig(device_poll_script=("authorization_pending", "ok"))
    ) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client,
                client_id=CLIENT_ID,
                scope="repo",
                device_authorization_endpoint=server.device_code_url,
            )
            assert grant.user_code
            assert grant.verification_uri == "https://github.com/login/device"

            pending = await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=server.device_token_url,
            )
            assert isinstance(pending, DeviceTokenPending)
            assert pending.reason == "authorization_pending"

            tokens = await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=server.device_token_url,
            )
            assert isinstance(tokens, TokenResponse)
            assert tokens.access_token.startswith("ghu_fake")
            assert tokens.refresh_token is not None
            assert tokens.expires_at is not None

            # The refresh is the third and last place a secret could sneak in.
            refreshed = await refresh_access_token(
                client,
                _metadata(server),
                credentials=ClientCredentials(
                    client_id=CLIENT_ID, token_endpoint_auth_method="none"
                ),
                refresh_token=tokens.refresh_token,
                resource=GITHUB_API_ORIGIN,
            )
            assert refreshed.access_token != tokens.access_token

        bodies = _every_body(server)
        assert len(bodies) == 4
        assert all("client_secret" not in body for body in bodies)
        assert all("redirect_uri" not in body for body in bodies)
        assert all(body.get("client_id") == CLIENT_ID for body in bodies)


async def test_every_token_request_asks_for_json() -> None:
    """GitHub form-encodes its answers unless the request says otherwise."""
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=("ok",))) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client, client_id=CLIENT_ID, device_authorization_endpoint=server.device_code_url
            )
            await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=server.device_token_url,
            )
        assert server.token_requests
        for record in server.token_requests:
            assert "application/json" in str(record["accept"]).lower()


async def test_slow_down_raises_the_interval_and_it_never_comes_back_down() -> None:
    script = ("slow_down", "authorization_pending", "ok")
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=script)) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client, client_id=CLIENT_ID, device_authorization_endpoint=server.device_code_url
            )
            interval = grant.interval_seconds

            slowed = await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=server.device_token_url,
            )
            assert isinstance(slowed, DeviceTokenPending)
            assert slowed.reason == "slow_down"
            raised = next_poll_interval(interval, slowed)
            assert raised >= interval + 5

            pending = await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=server.device_token_url,
            )
            assert isinstance(pending, DeviceTokenPending)
            assert pending.reason == "authorization_pending"
            # An ordinary pending answer does not win the faster rate back.
            assert next_poll_interval(raised, pending) == raised


async def test_declining_on_github_is_its_own_answer() -> None:
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=("access_denied",))) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client, client_id=CLIENT_ID, device_authorization_endpoint=server.device_code_url
            )
            with pytest.raises(DeviceAuthorizationDenied) as excinfo:
                await poll_github_device_token(
                    client,
                    client_id=CLIENT_ID,
                    device_code=grant.device_code,
                    token_endpoint=server.device_token_url,
                )
    assert PROVIDER_PROSE not in str(excinfo.value)


async def test_an_expired_code_is_its_own_answer() -> None:
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=("expired_token",))) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client, client_id=CLIENT_ID, device_authorization_endpoint=server.device_code_url
            )
            with pytest.raises(DeviceCodeExpired) as excinfo:
                await poll_github_device_token(
                    client,
                    client_id=CLIENT_ID,
                    device_code=grant.device_code,
                    token_endpoint=server.device_token_url,
                )
    assert PROVIDER_PROSE not in str(excinfo.value)


@pytest.mark.parametrize("errors_use_http_200", [True, False])
async def test_device_flow_disabled_says_where_to_switch_it_on(errors_use_http_200: bool) -> None:
    """GitHub reports this with HTTP 200 as often as with 400; both are heard."""
    config = FakeGitHubOAuthConfig(
        device_flow_enabled=False, errors_use_http_200=errors_use_http_200
    )
    with _fake_github(config) as server:
        async with httpx.AsyncClient() as client:
            with pytest.raises(TokenError) as excinfo:
                await start_github_device_authorization(
                    client,
                    client_id=CLIENT_ID,
                    device_authorization_endpoint=server.device_code_url,
                )
    assert excinfo.value.error_code == "device_flow_disabled"
    assert "Enable Device Flow" in str(excinfo.value)
    assert PROVIDER_PROSE not in str(excinfo.value)


async def test_an_unknown_device_code_does_not_leak_github_prose() -> None:
    script = ("incorrect_device_code",)
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=script)) as server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client, client_id=CLIENT_ID, device_authorization_endpoint=server.device_code_url
            )
            with pytest.raises((TokenError, DeviceCodeExpired)) as excinfo:
                await poll_github_device_token(
                    client,
                    client_id=CLIENT_ID,
                    device_code=grant.device_code,
                    token_endpoint=server.device_token_url,
                )
    assert PROVIDER_PROSE not in str(excinfo.value)


def test_every_device_flow_message_is_one_of_ours() -> None:
    disabled = device_flow_message("device_flow_disabled")
    assert "Enable Device Flow" in disabled
    assert device_flow_message("incorrect_client_credentials") != disabled
    # An unrecognised code still gets a sentence, and it is still Jhin's.
    assert device_flow_message("unknown") == device_flow_message("something_new")
    assert "GitHub" in device_flow_message("unknown")


async def test_a_client_id_that_is_not_one_never_reaches_github() -> None:
    with _fake_github() as server:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                await start_github_device_authorization(
                    client,
                    client_id="Iv23li\r\nX-Injected: 1",
                    device_authorization_endpoint=server.device_code_url,
                )
        assert server.device_code_requests == []


async def test_an_endpoint_outside_the_allow_list_is_refused_before_the_request() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(EndpointPolicyError):
            await start_github_device_authorization(
                client,
                client_id=CLIENT_ID,
                device_authorization_endpoint="http://169.254.169.254/login/device/code",
            )
        with pytest.raises(EndpointPolicyError):
            await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code="fake-device-code",
                token_endpoint="http://10.0.0.1/login/oauth/access_token",
            )


async def test_a_device_flow_token_is_a_working_github_connection() -> None:
    """The point of the flow: what it produces is an ordinary connection."""
    with _fake_github(FakeGitHubOAuthConfig(device_poll_script=("ok",))) as auth_server:
        async with httpx.AsyncClient() as client:
            grant = await start_github_device_authorization(
                client,
                client_id=CLIENT_ID,
                device_authorization_endpoint=auth_server.device_code_url,
            )
            tokens = await poll_github_device_token(
                client,
                client_id=CLIENT_ID,
                device_code=grant.device_code,
                token_endpoint=auth_server.device_token_url,
            )
        assert isinstance(tokens, TokenResponse)

    with FakeGitHubServer(token=tokens.access_token) as api:
        _allow(api.base_url)
        health = await GitHubConnector().verify_connection(
            VerifyContext(
                auth_type=AUTH_DEVICE,
                credentials={"access_token": tokens.access_token},
                config={"base_url": api.base_url},
            )
        )
    assert health.ok
    assert health.details["auth"] == AUTH_DEVICE
    assert tokens.access_token not in health.message
