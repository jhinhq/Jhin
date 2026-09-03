"""Creating this instance's own GitHub App from a manifest, and finishing it.

Every refusal is decided before a pending row is minted and answers with a
sentence, not a 500. The manifest sends an install to ``/apps`` and never
asks GitHub to follow an install with a state-less authorization code. The
conversion stores exactly a ``client_secret_post`` registration.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.redirect import redirect_uri
from jhin_api.oauth.schemas import GitHubAppManifestIn
from jhin_api.settings import Settings
from jhin_connectors.testing.fake_github_oauth import FakeGitHubOAuthServer
from jhin_db.models import OAuthAuthorization
from jhin_domain import new_uuid7
from jhin_oauth.persistence import OAuthClientStore
from jhin_secrets import SecretCrypto

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
APP_URL = "https://jhin.example.com"
REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the instance's own origin off the resolver; the fake is added per test."""
    monkeypatch.setenv(ALLOWLIST_ENV, APP_URL)
    yield


def _allow(*origins: str) -> None:
    current = [entry for entry in os.environ.get(ALLOWLIST_ENV, "").split(",") if entry]
    os.environ[ALLOWLIST_ENV] = ",".join([*current, *origins])


def _settings(app_url: str = APP_URL) -> Settings:
    return Settings(_env_file=None, app_url=app_url)


async def _pending_rows(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(OAuthAuthorization))
    return int(count or 0)


async def _refused(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    payload: GitHubAppManifestIn,
    settings: Settings | None = None,
) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await service.start_github_app_manifest(
            session, crypto, admin_ctx, settings or _settings(), payload
        )
    return caught.value


async def test_an_app_name_github_would_refuse_is_a_400_and_writes_nothing(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    error = await _refused(session, crypto, admin_ctx, GitHubAppManifestIn(app_name="Jhin (x)"))
    assert error.status_code == 400
    assert error.detail.startswith("GitHub App names use letters, numbers")
    assert await _pending_rows(session) == 0


async def test_an_organization_that_is_not_a_login_is_a_400(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    error = await _refused(
        session, crypto, admin_ctx, GitHubAppManifestIn(app_name="Jhin", organization="bad--login")
    )
    assert error.status_code == 400
    assert error.detail.startswith("That is not a GitHub organization name.")
    assert "bad--login" not in error.detail
    assert await _pending_rows(session) == 0


async def test_a_loopback_origin_outside_the_allow_list_is_a_400_not_a_500(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    error = await _refused(
        session,
        crypto,
        admin_ctx,
        GitHubAppManifestIn(app_name="Jhin"),
        settings=_settings("http://localhost:3000"),
    )
    assert error.status_code == 400
    assert "loopback or plain-HTTP origin" in error.detail
    assert "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS" in error.detail
    assert await _pending_rows(session) == 0


async def test_the_manifest_sends_an_install_to_apps_and_never_a_stateless_code(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    out = await service.start_github_app_manifest(
        session, crypto, admin_ctx, _settings(), GitHubAppManifestIn(app_name="Jhin")
    )
    assert out.post_url == "https://github.com/settings/apps/new"
    assert out.manifest["setup_url"] == f"{APP_URL}/apps"
    assert out.manifest["request_oauth_on_install"] is False
    assert out.manifest["callback_urls"] == [redirect_uri(_settings())]
    assert out.manifest["redirect_url"] == f"{APP_URL}/api/v1/oauth/github-app/callback"
    assert out.manifest["public"] is False
    assert await _pending_rows(session) == 1


async def test_the_conversion_stores_a_confidential_registration(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    settings = _settings()
    started = await service.start_github_app_manifest(
        session, crypto, admin_ctx, settings, GitHubAppManifestIn(app_name="Jhin")
    )

    with FakeGitHubOAuthServer() as fake:
        _allow(fake.base_url)
        async with httpx.AsyncClient(base_url=fake.base_url) as http_client:
            created = await service.complete_github_app_manifest(
                session,
                crypto,
                http_client,
                settings,
                user_id=admin_ctx.user.id,
                state=started.state,
                code="fake-manifest-code",
                **REQ,  # type: ignore[arg-type]
            )

    assert created is True
    found = await OAuthClientStore(session, crypto).get(
        admin_ctx.workspace_id,
        issuer="https://github.com",
        redirect_uri=redirect_uri(settings),
    )
    assert found is not None
    row, credentials = found
    assert row.source == "manual"
    assert credentials.token_endpoint_auth_method == "client_secret_post"
    assert credentials.client_id.startswith("Iv1.fake")
    assert credentials.client_secret
    assert credentials.client_secret.startswith("fake-github-client-secret-")
    # The row is spent: a second arrival with the same state is refused.
    assert await _pending_rows(session) == 0
