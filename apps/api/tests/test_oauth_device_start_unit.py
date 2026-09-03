"""What a person is told when a provider refuses to start a device sign-in.

A device sign-in fails at start for a few nameable reasons, and the fix for
each is on the provider's side. GitHub Apps ship with the device flow switched
off, so the common refusal is ``device_flow_disabled``; the message has to say
which checkbox to tick, not "the provider refused".
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
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> None:
    await service.create_client(
        session,
        crypto,
        admin_ctx,
        _settings(),
        OAuthClientCreate(
            issuer=STATIC_PROVIDERS["github"].issuer,
            client_id="Iv23lixxxxxxxxxxxxxx",
            client_secret=None,
            token_endpoint_auth_method="none",
            scopes="repo",
        ),
        **REQ,  # type: ignore[arg-type]
    )


def _refusing(exc: Exception) -> Any:
    async def start(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return start


async def _start(
    session: AsyncSession, crypto: SecretCrypto, admin_ctx: WorkspaceContext
) -> HTTPException:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(HTTPException) as caught:
            await service.start_device_flow(
                session,
                crypto,
                admin_ctx,
                http_client,
                _settings(),
                OAuthDeviceStartIn(connector_type="github", name="GitHub"),
            )
    return caught.value


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
