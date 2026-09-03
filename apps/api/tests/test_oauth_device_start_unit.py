"""What a person is told when a provider refuses to start a device sign-in.

A device sign-in fails at start for a few nameable reasons. GitHub Apps ship
with the device flow switched off, so the common refusal is
``device_flow_disabled``. When the registration can do the browser sign-in —
a secret is stored — the message points at that, one click away and needing
no change on GitHub; only when it cannot does the message name the checkbox.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.oauth import service
from jhin_api.oauth.schemas import OAuthClientCreate, OAuthDeviceStartIn
from jhin_api.settings import Settings
from jhin_connectors.oauth_providers import STATIC_PROVIDERS
from jhin_domain import new_uuid7
from jhin_oauth.errors import ClientForgottenError, TokenError
from jhin_secrets import SecretCrypto

REQ = {"request_id": new_uuid7(), "ip_hash": "0" * 64}


def _settings() -> Settings:
    return Settings(_env_file=None, app_url="https://jhin.example.com")


async def _register_github_app(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    *,
    secret: str | None = None,
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
            scopes="repo",
        ),
        **REQ,  # type: ignore[arg-type]
    )


def _refusing(exc: Exception) -> Any:
    async def start(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return start


async def _start(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    config: dict[str, Any] | None = None,
) -> HTTPException:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(HTTPException) as caught:
            await service.start_device_flow(
                session,
                crypto,
                admin_ctx,
                http_client,
                _settings(),
                OAuthDeviceStartIn(connector_type="github", name="GitHub", config=config or {}),
            )
    return caught.value


async def test_a_github_app_with_the_device_flow_off_is_offered_the_browser_instead(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret is stored, so the browser sign-in works: say that, not the checkbox."""
    await _register_github_app(session, crypto, admin_ctx, secret="not-a-real-secret-0000")
    monkeypatch.setattr(
        service,
        "start_device_authorization",
        _refusing(TokenError("device sign-in is turned off", error_code="device_flow_disabled")),
    )

    error = await _start(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert error.detail.startswith("GitHub has device sign-in turned off for this app.")
    assert "Use the browser sign-in instead" in error.detail
    assert "needs no change on GitHub" in error.detail
    assert "Enable Device Flow" not in error.detail


async def test_an_app_not_allowed_the_device_flow_is_offered_the_browser_when_it_can(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_github_app(session, crypto, admin_ctx, secret="not-a-real-secret-0000")
    monkeypatch.setattr(
        service,
        "start_device_authorization",
        _refusing(TokenError("not allowed", error_code="unauthorized_client")),
    )

    error = await _start(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert error.detail.startswith("GitHub does not allow this app to use the device sign-in.")
    assert "Use the browser sign-in instead" in error.detail


async def test_no_registration_points_at_connect_on_apps(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    error = await _start(session, crypto, admin_ctx)
    assert error.status_code == 409
    assert "Register it from Apps" in error.detail


async def test_a_github_enterprise_address_is_refused_before_any_request(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_github_app(session, crypto, admin_ctx, secret="not-a-real-secret-0000")
    monkeypatch.setattr(
        service, "start_device_authorization", _refusing(AssertionError("must not be reached"))
    )

    error = await _start(session, crypto, admin_ctx, {"base_url": "https://ghe.example.com/api/v3"})

    assert error.status_code == 400
    assert "GitHub Enterprise" in error.detail
    assert "ghe.example.com" not in error.detail


async def test_a_github_app_with_the_device_flow_off_is_told_which_box_to_tick(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_github_app(session, crypto, admin_ctx)
    monkeypatch.setattr(
        service,
        "start_device_authorization",
        _refusing(TokenError("device sign-in is turned off", error_code="device_flow_disabled")),
    )

    error = await _start(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert "Enable Device Flow" in error.detail
    assert error.detail.startswith("GitHub has device sign-in turned off for this app.")


async def test_a_forgotten_client_points_at_the_oauth_settings(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_github_app(session, crypto, admin_ctx)
    monkeypatch.setattr(
        service,
        "start_device_authorization",
        _refusing(ClientForgottenError("the registration is gone")),
    )

    error = await _start(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert "no longer recognises this app's client id" in error.detail
    assert "Settings → OAuth" in error.detail


async def test_a_refusal_jhin_cannot_name_stays_generic_and_never_quotes_the_provider(
    session: AsyncSession,
    crypto: SecretCrypto,
    admin_ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register_github_app(session, crypto, admin_ctx)
    monkeypatch.setattr(
        service,
        "start_device_authorization",
        _refusing(TokenError("the authorization server refused the token request")),
    )

    error = await _start(session, crypto, admin_ctx)

    assert error.status_code == 400
    assert error.detail == "The provider refused to start a device sign-in for this app."
