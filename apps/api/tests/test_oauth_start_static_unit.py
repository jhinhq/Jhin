"""Starting the browser sign-in for a native provider.

GitHub's redirect flow needs nothing on GitHub's side beyond the app existing
with a client secret, and this is the path Connect GitHub takes first. What
is pinned here: the authorization URL carries state and an S256 challenge and
no secret; a registration that cannot do the redirect is refused at *start*
with the fix named, not at the exchange after the person said yes; and a
GitHub Enterprise address is refused in Jhin's words rather than GitHub's.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.schemas import OAuthClientCreate, OAuthStartIn
from jhin_api.settings import Settings
from jhin_connectors import oauth_providers
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.oauth_providers import STATIC_PROVIDERS
from jhin_db.models import OAuthAuthorization
from jhin_domain import new_uuid7
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}
FAKE_SECRET = "not-a-real-github-client-secret-0000"


def _settings() -> Settings:
    return Settings(_env_file=None, app_url="https://jhin.example.com")


async def _register(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    *,
    secret: str | None,
) -> None:
    await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(
            issuer=STATIC_PROVIDERS["github"].issuer,
            client_id="Iv23lixxxxxxxxxxxxxx",
            client_secret=secret,
            token_endpoint_auth_method="client_secret_post" if secret else "none",
        ),
        **REQ,  # type: ignore[arg-type]
    )


async def _start(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    config: dict[str, Any] | None = None,
) -> Any:
    async with httpx.AsyncClient() as http_client:
        return await service.start_authorization(
            session,
            crypto,
            admin_ctx,
            http_client,
            _settings(),
            OAuthStartIn(connector_type="github", name="GitHub", config=config or {}),
            **REQ,  # type: ignore[arg-type]
        )


async def _refused(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    config: dict[str, Any] | None = None,
) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _start(session, crypto, admin_ctx, config)
    return caught.value


async def test_a_registered_app_with_a_secret_starts_the_browser_sign_in(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET)

    started = await _start(session, crypto, admin_ctx)

    parsed = urlsplit(started.authorization_url)
    assert parsed.hostname == "github.com"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["Iv23lixxxxxxxxxxxxxx"]
    assert query["state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"]
    # A GitHub App's access comes from its installation, so no scope; and no
    # resource concept at all, so no RFC 8707 parameter.
    assert "scope" not in query
    assert "resource" not in query
    assert "client_secret" not in query
    assert FAKE_SECRET not in started.authorization_url
    assert started.issuer == "https://github.com"
    assert started.client_source == "manual"

    row = await session.scalar(
        select(OAuthAuthorization).where(OAuthAuthorization.workspace_id == admin_ctx.workspace_id)
    )
    assert row is not None
    assert row.flow == "authorization_code"
    assert row.connector_type == "github"
    assert row.redirect_uri == "https://jhin.example.com/api/v1/oauth/callback"


async def test_a_registration_without_a_secret_is_refused_before_the_redirect(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """Earlier and clearer than a failed exchange: the fix is one paste away."""
    await _register(session, crypto, admin_ctx, secret=None)

    error = await _refused(session, crypto, admin_ctx)

    assert error.status_code == 409
    assert "no client secret" in error.detail
    assert "github.com" in error.detail
    assert "Apps → Connect GitHub" in error.detail
    assert "sign in with a code" in error.detail
    rows = (await session.scalars(select(OAuthAuthorization))).all()
    assert rows == []


async def test_no_registration_points_at_connect_on_apps(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    error = await _refused(session, crypto, admin_ctx)

    assert error.status_code == 409
    assert "Register it from Apps" in error.detail
    assert "GitHub" in error.detail
    # Settings → OAuth has no form; sending someone there was a dead end.
    assert "Settings → OAuth" not in error.detail


async def test_a_github_enterprise_address_is_refused_in_jhins_words(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET)

    error = await _refused(
        session, crypto, admin_ctx, {"base_url": "https://ghe.example.com/api/v3"}
    )

    assert error.status_code == 400
    assert "GitHub Enterprise" in error.detail
    assert "personal access token" in error.detail
    assert "ghe.example.com" not in error.detail


async def test_githubs_own_api_origin_is_not_an_enterprise_server(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """The default ``base_url`` a reconnect carries must not trip the check."""
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET)
    started = await _start(session, crypto, admin_ctx, {"base_url": "https://api.github.com"})
    assert started.authorization_url.startswith("https://github.com/login/oauth/authorize?")


async def test_a_provider_url_the_policy_refuses_is_a_400_not_a_500(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET)

    def _refusing(_provider: Any) -> Any:
        raise EndpointPolicyError("github authorization endpoint is not allowed")

    monkeypatch.setattr(oauth_providers, "provider_metadata", _refusing)

    error = await _refused(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert "not one this Jhin install is allowed to reach" in error.detail
