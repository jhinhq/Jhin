"""What the probe answers for a native provider, and why it never guesses.

The rule under test: the browser sign-in comes first whenever the
registration can do it; the sign-in code is reported as available beside it,
never instead of it. GitHub's device endpoint existing is not a reason to
route to the device flow — a GitHub App starts with that flow switched off,
and the redirect needs no toggle on GitHub at all. The only things that put
the code first are a registration with no secret (the redirect cannot start)
and an operator who asked with ``OAUTH_PREFER_DEVICE_CODE``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.schemas import OAuthClientCreate, OAuthProbeIn, OAuthProbeOut
from jhin_api.settings import Settings
from jhin_connectors.oauth_providers import STATIC_PROVIDERS
from jhin_domain import new_uuid7
from jhin_oauth.discovery import McpAuthProbe
from jhin_oauth.types import AuthorizationServerMetadata
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}
FAKE_SECRET = "not-a-real-github-client-secret-0000"
GITHUB_APPS_PAGE = "https://github.com/settings/apps"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"app_url": "https://jhin.example.com"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _register(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    *,
    secret: str | None,
    settings: Settings | None = None,
) -> None:
    await service.create_client(
        session,
        crypto,
        admin_ctx,
        settings or _settings(),
        OAuthClientCreate(
            issuer=STATIC_PROVIDERS["github"].issuer,
            client_id="Iv23lixxxxxxxxxxxxxx",
            client_secret=secret,
            token_endpoint_auth_method="client_secret_post" if secret else "none",
        ),
        **REQ,  # type: ignore[arg-type]
    )


async def _probe(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    *,
    connector_type: str = "github",
    settings: Settings | None = None,
    server_url: str | None = None,
) -> OAuthProbeOut:
    async with httpx.AsyncClient() as http_client:
        return await service.probe(
            session,
            crypto,
            admin_ctx,
            http_client,
            settings or _settings(),
            OAuthProbeIn(connector_type=connector_type, server_url=server_url),
        )


async def test_nothing_registered_asks_for_the_app_and_says_where_to_make_one(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    probe = await _probe(session, crypto, admin_ctx)

    assert probe.method == "oauth_needs_client"
    assert probe.reason == "needs_client_credentials"
    assert probe.client_configured is False
    assert probe.supports_oauth is True
    assert probe.supports_dcr is False
    assert probe.issuer == "https://github.com"
    assert probe.authorization_server_display == "github.com"
    assert probe.redirect_flow.model_dump() == {
        "available": False,
        "reason": "needs_client_credentials",
    }
    assert probe.device_flow.model_dump() == {
        "available": False,
        "reason": "needs_client_credentials",
    }
    assert probe.app_settings_url == GITHUB_APPS_PAGE


async def test_a_registration_with_a_secret_goes_to_the_browser_first(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET)

    probe = await _probe(session, crypto, admin_ctx)

    assert probe.method == "oauth_static"
    assert probe.reason == ""
    assert probe.client_configured is True
    assert probe.redirect_flow.available is True
    assert probe.device_flow.available is True
    assert probe.requires_client_secret is True
    assert FAKE_SECRET not in probe.model_dump_json()


async def test_a_registration_without_a_secret_goes_to_the_code_and_says_why(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """The redirect cannot start without a secret; the code can. Both are said."""
    await _register(session, crypto, admin_ctx, secret=None)

    probe = await _probe(session, crypto, admin_ctx)

    assert probe.method == "device_code"
    assert probe.reason == "needs_client_secret"
    assert probe.client_configured is True
    assert probe.redirect_flow.model_dump() == {"available": False, "reason": "needs_client_secret"}
    assert probe.device_flow.available is True


async def test_a_loopback_instance_is_not_a_reason_to_demote_the_browser(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    """GitHub redirects the browser that just loaded Jhin; localhost is fine."""
    settings = _settings(app_url="http://localhost:3000")
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET, settings=settings)

    probe = await _probe(session, crypto, admin_ctx, settings=settings)

    assert probe.method == "oauth_static"
    assert probe.redirect_flow.available is True


async def test_the_operator_can_put_the_code_first_without_losing_the_browser(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    settings = _settings(oauth_prefer_device_code=True)
    await _register(session, crypto, admin_ctx, secret=FAKE_SECRET, settings=settings)

    probe = await _probe(session, crypto, admin_ctx, settings=settings)

    assert probe.method == "device_code"
    assert probe.reason == ""
    assert probe.redirect_flow.available is True
    assert probe.device_flow.available is True


async def test_a_connector_with_no_provider_entry_is_an_api_key(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    probe = await _probe(session, crypto, admin_ctx, connector_type="linear")

    assert probe.method == "api_key"
    assert probe.supports_oauth is False
    assert probe.reason == "connector_has_no_oauth"
    assert probe.redirect_flow.available is False
    assert probe.device_flow.available is False
    assert probe.app_settings_url == ""


async def test_an_mcp_server_that_registers_clients_reports_the_redirect_only(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = AuthorizationServerMetadata(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        code_challenge_methods_supported=("S256",),
    )

    async def _discovered(_client: httpx.AsyncClient, server_url: str) -> McpAuthProbe:
        return McpAuthProbe(
            server_url=server_url,
            requires_auth=True,
            resource_metadata_url=None,
            challenge_scope=None,
            protected_resource=None,
            authorization_server=metadata,
            supports_oauth=True,
            supports_dcr=True,
            failure_reason=None,
        )

    monkeypatch.setattr(service, "probe_mcp_endpoint", _discovered)

    probe = await _probe(
        session, crypto, admin_ctx, connector_type="mcp", server_url="https://mcp.example.com/mcp"
    )

    assert probe.method == "oauth_discovery"
    assert probe.redirect_flow.available is True
    assert probe.device_flow.model_dump() == {"available": False, "reason": "no_device_endpoint"}
    assert probe.app_settings_url == ""


@pytest.mark.parametrize(
    ("secret", "prefer_device"),
    [(FAKE_SECRET, False), (FAKE_SECRET, True), (None, False), (None, True)],
)
async def test_the_code_comes_first_only_when_asked_or_when_the_browser_cannot_start(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    secret: str | None,
    prefer_device: bool,
) -> None:
    """The one rule, over every registration/setting combination."""
    settings = _settings(oauth_prefer_device_code=prefer_device)
    await _register(session, crypto, admin_ctx, secret=secret, settings=settings)

    probe = await _probe(session, crypto, admin_ctx, settings=settings)

    assert (probe.method == "device_code") is (prefer_device or secret is None)
    # Whichever comes first, the other is reported rather than removed.
    assert probe.device_flow.available is True
    assert probe.redirect_flow.available is (secret is not None)
