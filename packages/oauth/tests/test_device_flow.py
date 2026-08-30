"""RFC 8628 device flow: the path that needs no redirect URI at all.

This is the answer for an instance a provider cannot reach — a laptop, a
private network, anything without TLS — so it has to work with no client secret
at any step, and it has to survive GitHub's habit of returning polling errors
with ``HTTP 200`` and the error in the body. A client that only reads the
status code treats ``authorization_pending`` as a success with no access token,
which is how a device flow silently hangs forever.
"""

from __future__ import annotations

import httpx
import pytest
from packages.oauth.tests.conftest import StartServer

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_oauth.errors import (
    DeviceAuthorizationDenied,
    DeviceCodeExpired,
    TokenError,
    TransientOAuthError,
)
from jhin_oauth.tokens import (
    DEFAULT_DEVICE_INTERVAL_SECONDS,
    SLOW_DOWN_INCREMENT_SECONDS,
    next_poll_interval,
    poll_device_token,
    start_device_authorization,
)
from jhin_oauth.types import DeviceCodeGrant, DeviceTokenPending, TokenResponse
from jhin_secrets.redaction import get_redactor

CLIENT_ID = "fake-device-client"


def _device_server(start_server: StartServer, **overrides: object) -> FakeAuthorizationServer:
    server = start_server(FakeAsConfig(supports_device_flow=True, **overrides))
    server.register_static_client(client_id=CLIENT_ID)
    return server


async def _grant(
    http_client: httpx.AsyncClient, server: FakeAuthorizationServer, *, scope: str = ""
) -> DeviceCodeGrant:
    return await start_device_authorization(
        http_client,
        device_authorization_endpoint=server.device_authorization_endpoint,
        client_id=CLIENT_ID,
        scope=scope,
    )


async def _poll(
    http_client: httpx.AsyncClient,
    server: FakeAuthorizationServer,
    grant: DeviceCodeGrant,
    *,
    client_secret: str | None = None,
) -> TokenResponse | DeviceTokenPending:
    return await poll_device_token(
        http_client,
        token_endpoint=server.token_endpoint,
        client_id=CLIENT_ID,
        device_code=grant.device_code,
        client_secret=client_secret,
    )


async def test_device_authorization_returns_something_a_person_can_act_on(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server, scope="read")

    assert isinstance(grant, DeviceCodeGrant)
    assert grant.user_code
    assert grant.verification_uri.startswith("http")
    assert grant.verification_uri_complete is not None
    assert grant.interval_seconds >= DEFAULT_DEVICE_INTERVAL_SECONDS
    assert grant.expires_at is not None
    # The device code is credential material; the user code is a display code
    # and worthless on its own.
    assert grant.device_code not in get_redactor().redact_text(f"log {grant.device_code}")


async def test_the_device_start_sends_no_client_secret(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    await _grant(http_client, server)
    body = server.recorded_requests(path_suffix="/device/code")[-1]["body"]
    assert body["client_id"] == CLIENT_ID
    assert "client_secret" not in body


async def test_polling_before_approval_is_pending(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)

    result = await _poll(http_client, server, grant)

    assert isinstance(result, DeviceTokenPending)
    assert result.reason == "authorization_pending"
    assert result.interval_seconds == DEFAULT_DEVICE_INTERVAL_SECONDS


async def test_polling_sends_no_client_secret_when_none_is_configured(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)
    await _poll(http_client, server, grant)

    body = server.recorded_requests(path_suffix="/token")[-1]["body"]
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert body["client_id"] == CLIENT_ID
    assert "client_secret" not in body


async def test_polling_after_approval_returns_tokens(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)
    server.approve_device(grant.user_code)

    result = await _poll(http_client, server, grant)

    assert isinstance(result, TokenResponse)
    assert result.access_token
    assert result.access_token not in get_redactor().redact_text(f"log {result.access_token}")


async def test_slow_down_raises_the_interval_and_it_never_comes_back_down(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server, slow_down_once=True)
    grant = await _grant(http_client, server)

    first = await _poll(http_client, server, grant)
    assert isinstance(first, DeviceTokenPending)
    assert first.reason == "slow_down"
    assert first.interval_seconds >= DEFAULT_DEVICE_INTERVAL_SECONDS + SLOW_DOWN_INCREMENT_SECONDS

    interval = next_poll_interval(grant.interval_seconds, first)
    assert interval == grant.interval_seconds + SLOW_DOWN_INCREMENT_SECONDS

    second = await _poll(http_client, server, grant)
    assert isinstance(second, DeviceTokenPending)
    assert second.reason == "authorization_pending"
    # RFC 8628 §3.5 makes the increase permanent: a client that returns to its
    # old cadence gets throttled again and eventually rejected.
    assert next_poll_interval(interval, second) == interval


async def test_a_declined_request_is_reported_as_denied(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)
    server.deny_device(grant.user_code)

    with pytest.raises(DeviceAuthorizationDenied):
        await _poll(http_client, server, grant)


async def test_an_expired_code_is_reported_as_expired(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)
    server.expire_device(grant.user_code)

    with pytest.raises(DeviceCodeExpired):
        await _poll(http_client, server, grant)


async def test_an_unknown_device_code_is_expired_not_a_crash(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    grant = await _grant(http_client, server)
    unknown = DeviceCodeGrant(
        device_code="fake-device-code-that-was-never-issued",
        user_code=grant.user_code,
        verification_uri=grant.verification_uri,
        verification_uri_complete=None,
        expires_at=grant.expires_at,
    )
    with pytest.raises(DeviceCodeExpired):
        await _poll(http_client, server, unknown)


# --- GitHub's shape --------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [("pending", None), ("deny", DeviceAuthorizationDenied), ("expire", DeviceCodeExpired)],
)
async def test_errors_returned_with_http_200_are_still_errors(
    http_client: httpx.AsyncClient,
    start_server: StartServer,
    action: str,
    expected: type[Exception] | None,
) -> None:
    # GitHub answers a pending device grant with HTTP 200 and the error in the
    # body. Reading only the status code turns that into "success, no token".
    server = _device_server(start_server, device_errors_with_http_200=True)
    grant = await _grant(http_client, server)
    if action == "deny":
        server.deny_device(grant.user_code)
    elif action == "expire":
        server.expire_device(grant.user_code)

    if expected is None:
        result = await _poll(http_client, server, grant)
        assert isinstance(result, DeviceTokenPending)
        assert result.reason == "authorization_pending"
    else:
        with pytest.raises(expected):
            await _poll(http_client, server, grant)


async def test_slow_down_with_http_200_is_still_slow_down(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server, device_errors_with_http_200=True, slow_down_once=True)
    grant = await _grant(http_client, server)
    result = await _poll(http_client, server, grant)
    assert isinstance(result, DeviceTokenPending)
    assert result.reason == "slow_down"


# --- refusals --------------------------------------------------------------


async def test_a_server_with_device_flow_off_says_so(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(supports_device_flow=False))
    server.register_static_client(client_id=CLIENT_ID)

    with pytest.raises(TokenError) as caught:
        await _grant(http_client, server)
    assert caught.value.error_code == "device_flow_disabled"
    assert "fake failure" not in str(caught.value)


async def test_an_unreachable_device_endpoint_is_transient(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = _device_server(start_server)
    endpoint = server.device_authorization_endpoint
    server.stop()

    with pytest.raises(TransientOAuthError):
        await start_device_authorization(
            http_client, device_authorization_endpoint=endpoint, client_id=CLIENT_ID
        )
