"""The browser sign-in for GitHub, walked end to end against the fake.

This is the test the defect lacked: nothing exercised ``oauth_static`` from
``start_authorization`` through GitHub's redirect to the callback and the
exchange, because the probe never chose it. Now the whole path runs — the
authorization URL is fetched from the fake's ``/authorize``, the ``Location``
it answers with is walked into the real callback route, the code is
exchanged with the stored secret and the PKCE verifier, and a connection
comes out the other side with ``auth_type="oauth"``.

The two first-setup mistakes a person can fix are walked too: a wrong client
secret and a callback URL the app does not list, both of which GitHub reports
with HTTP 200 and the error in the body, and both of which land on Apps with
a flag from the closed set rather than a generic failure.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from apps.api.tests.oauth_callback_harness import CALLBACK_APP_URL, CallbackHarness
from sqlalchemy import select

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.redirect import CALLBACK_PATH, redirect_uri
from jhin_api.oauth.schemas import OAuthClientCreate, OAuthStartIn
from jhin_api.settings import Settings
from jhin_connectors.oauth_providers import STATIC_PROVIDERS
from jhin_connectors.testing.fake_github_oauth import (
    FakeGitHubOAuthConfig,
    FakeGitHubOAuthServer,
)
from jhin_db.models import Connection
from jhin_domain import WorkspaceRole, new_uuid7

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
RIGHT_SECRET = "fake-github-client-secret-right"
WRONG_SECRET = "fake-github-client-secret-wrong"
CONNECTED = re.compile(r"/apps\?connection=([0-9a-f]{32})$")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubOAuthServer]:
    """GitHub's OAuth surface on loopback, with the provider table pointed at it.

    The issuer and the device endpoint stay github.com — the fake replaces
    only where the browser is sent and where the code is exchanged, which is
    what a real GitHub App differs in from the table by exactly nothing.
    """
    with FakeGitHubOAuthServer(FakeGitHubOAuthConfig(expected_client_secret=RIGHT_SECRET)) as fake:
        monkeypatch.setenv(ALLOWLIST_ENV, fake.base_url)
        provider = dataclasses.replace(
            STATIC_PROVIDERS["github"],
            authorization_endpoint=fake.authorize_url,
            token_endpoint=fake.device_token_url,
        )
        monkeypatch.setattr(service, "_static_provider_for", lambda _connector_type: provider)
        yield fake


def _settings() -> Settings:
    return Settings(_env_file=None, app_url=CALLBACK_APP_URL)


def _ctx(callback: CallbackHarness) -> WorkspaceContext:
    return WorkspaceContext(
        user=callback.admin, workspace_id=callback.workspace_id, role=WorkspaceRole.ADMIN
    )


async def _register(callback: CallbackHarness, *, secret: str) -> Any:
    return await service.create_client(
        callback.session,
        callback.crypto,
        _ctx(callback),
        _settings(),
        OAuthClientCreate(
            issuer=STATIC_PROVIDERS["github"].issuer,
            client_id="Iv23lixxxxxxxxxxxxxx",
            client_secret=secret,
            token_endpoint_auth_method="client_secret_post",
        ),
        request_id=new_uuid7(),
        ip_hash="0" * 64,
    )


async def _sign_in(callback: CallbackHarness) -> httpx.Response:
    """Start, visit the provider, and walk its redirect into the callback."""
    async with httpx.AsyncClient() as http_client:
        started = await service.start_authorization(
            callback.session,
            callback.crypto,
            _ctx(callback),
            http_client,
            _settings(),
            OAuthStartIn(connector_type="github", name="GitHub"),
            request_id=new_uuid7(),
            ip_hash="0" * 64,
        )
        assert RIGHT_SECRET not in started.authorization_url
        assert WRONG_SECRET not in started.authorization_url
        consent = await http_client.get(started.authorization_url, follow_redirects=False)
    assert consent.status_code == 302
    sent_back_to = urlsplit(consent.headers["location"])
    assert f"{sent_back_to.scheme}://{sent_back_to.netloc}{sent_back_to.path}" == redirect_uri(
        _settings()
    )
    query = parse_qs(sent_back_to.query)
    return await callback.client.get(
        CALLBACK_PATH, params={"state": query["state"][0], "code": query["code"][0]}
    )


async def _connections(callback: CallbackHarness) -> list[Connection]:
    rows = await callback.session.scalars(
        select(Connection).where(Connection.workspace_id == callback.workspace_id)
    )
    return list(rows)


async def test_continue_authorize_connected(
    callback: CallbackHarness, fake: FakeGitHubOAuthServer
) -> None:
    registration = await _register(callback, secret=RIGHT_SECRET)

    response = await _sign_in(callback)

    assert response.status_code == 303
    landed = CONNECTED.search(response.headers["location"])
    assert landed is not None, response.headers["location"]

    (connection,) = await _connections(callback)
    assert connection.public_id == landed.group(1)
    assert connection.connector_type == "github"
    assert connection.auth_type == "oauth"
    assert connection.oauth_issuer == "https://github.com"
    assert connection.oauth_client_registration_id == registration.id
    assert connection.oauth_authorized_by_user_id == callback.admin.id

    body = fake.recorded_bodies()[-1]
    assert body["grant_type"] == "authorization_code"
    assert body["client_secret"] == RIGHT_SECRET
    assert body["code_verifier"]
    assert body["redirect_uri"] == redirect_uri(_settings())
    # A provider with no resource concept gets no RFC 8707 parameter.
    assert "resource" not in body


async def test_walking_the_same_callback_url_twice_connects_once(
    callback: CallbackHarness, fake: FakeGitHubOAuthServer
) -> None:
    """A refresh, a back-button, or a prefetch that beat the navigation.

    The state is single-use and stays single-use: the second request reaches
    no token endpoint at all. It is answered from the receipt the first one
    left, so the person lands on the connection they made rather than on a
    dead end telling them their sign-in link is no longer valid.
    """
    await _register(callback, secret=RIGHT_SECRET)

    first = await _sign_in(callback)
    assert first.status_code == 303
    exchanges = len(fake.recorded_bodies())

    url = str(first.request.url)
    second = await callback.client.get(url)

    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert len(await _connections(callback)) == 1
    assert len(fake.recorded_bodies()) == exchanges, "the code was exchanged twice"


async def test_a_wrong_client_secret_lands_on_client_rejected_and_connects_nothing(
    callback: CallbackHarness, fake: FakeGitHubOAuthServer
) -> None:
    await _register(callback, secret=WRONG_SECRET)

    response = await _sign_in(callback)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/apps?oauth_error=client_rejected&app=github")
    assert await _connections(callback) == []
    assert fake.recorded_bodies()[-1]["client_secret"] == WRONG_SECRET


async def test_a_callback_url_the_app_does_not_list_lands_on_callback_mismatch(
    callback: CallbackHarness, fake: FakeGitHubOAuthServer
) -> None:
    await _register(callback, secret=RIGHT_SECRET)
    fake.refuse_next_exchange("redirect_uri_mismatch")

    response = await _sign_in(callback)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/apps?oauth_error=callback_mismatch&app=github")
    assert await _connections(callback) == []
    # Never the fake's own prose.
    assert "fake GitHub" not in response.headers["location"]
