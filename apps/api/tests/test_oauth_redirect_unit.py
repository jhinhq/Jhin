"""The redirect URI is one constant, and the return URL is not attacker-shaped.

Two properties, both structural rather than defensive:

* the callback URI an operator registers with a provider is derived from
  settings and nothing else, and a base URL that cannot host one is refused at
  startup rather than discovered when a provider rejects an authorization;
* ``app_return_url`` is the only builder of a ``Location`` header for a
  browser leaving the callback, and it has no parameter through which a
  request-supplied string could reach it.
"""

from __future__ import annotations

import pytest

from jhin_api.oauth.redirect import (
    CALLBACK_PATH,
    GITHUB_APP_CALLBACK_PATH,
    OAuthRedirectMisconfigured,
    app_return_url,
    configured_via,
    github_app_redirect_uri,
    is_https_redirect,
    is_loopback_redirect,
    redirect_base,
    redirect_uri,
)
from jhin_api.settings import InsecureDeploymentError, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"app_url": "http://localhost:3000"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_redirect_uri_defaults_to_app_url() -> None:
    settings = _settings(app_url="https://jhin.example.com")
    assert redirect_uri(settings) == f"https://jhin.example.com{CALLBACK_PATH}"
    assert github_app_redirect_uri(settings) == (
        f"https://jhin.example.com{GITHUB_APP_CALLBACK_PATH}"
    )
    assert configured_via(settings) == "APP_URL"


def test_explicit_base_url_wins_over_app_url() -> None:
    settings = _settings(
        app_url="https://app.example.com",
        oauth_redirect_base_url="https://api.example.com",
    )
    assert redirect_uri(settings) == f"https://api.example.com{CALLBACK_PATH}"
    assert configured_via(settings) == "OAUTH_REDIRECT_BASE_URL"


def test_trailing_slash_and_port_normalize_to_one_string() -> None:
    """Providers match the registered URI exactly, so ours must be stable."""
    assert redirect_base(_settings(app_url="https://jhin.example.com/")) == (
        "https://jhin.example.com"
    )
    assert redirect_base(_settings(app_url="http://localhost:3000")) == "http://localhost:3000"


@pytest.mark.parametrize(
    "base",
    [
        "https://jhin.example.com/jhin",
        "https://jhin.example.com/?next=x",
        "https://jhin.example.com/#frag",
        "https://user:pass@jhin.example.com",
        "not-a-url",
        "ftp://jhin.example.com",
    ],
)
def test_a_base_that_cannot_host_a_redirect_is_refused_at_startup(base: str) -> None:
    with pytest.raises(InsecureDeploymentError):
        _settings(oauth_redirect_base_url=base)


def test_plaintext_http_on_a_public_host_is_refused_in_production() -> None:
    with pytest.raises(InsecureDeploymentError):
        _settings(
            app_env="production",
            app_url="https://jhin.example.com",
            cookie_secure=True,
            oauth_redirect_base_url="http://jhin.example.com",
        )


def test_plaintext_loopback_is_allowed_in_production() -> None:
    """A single-machine install reached only over loopback is legitimate."""
    settings = _settings(
        app_env="production",
        app_url="https://jhin.example.com",
        cookie_secure=True,
        oauth_redirect_base_url="http://127.0.0.1:8000",
    )
    assert redirect_uri(settings) == f"http://127.0.0.1:8000{CALLBACK_PATH}"
    assert is_loopback_redirect(settings) is True
    assert is_https_redirect(settings) is False


def test_redirect_base_refuses_a_broken_base_at_use_time_too() -> None:
    """Settings validation is the first gate, not the only one.

    ``model_construct`` skips validators, which is exactly the situation this
    guard exists for: no path reaches a provider with a URI this module has
    not itself approved.
    """
    settings = Settings.model_construct(app_url="https://evil.example.com/steal?x=1")
    with pytest.raises(OAuthRedirectMisconfigured):
        redirect_base(settings)


def test_app_return_url_is_built_from_settings_and_a_proven_public_id() -> None:
    settings = _settings(app_url="https://jhin.example.com/")
    assert app_return_url(settings, public_id="0" * 32) == (
        "https://jhin.example.com/apps?connection=" + "0" * 32
    )
    assert app_return_url(settings, public_id=None) == "https://jhin.example.com/apps"
    assert app_return_url(settings, public_id=None, error="denied") == (
        "https://jhin.example.com/apps?oauth_error=denied"
    )


@pytest.mark.parametrize(
    "public_id",
    [
        "",
        "not-hex",
        "0" * 31,
        "0" * 33,
        "0" * 31 + "G",
        "https://evil.example",
        "../../evil",
        "0" * 32 + "?next=https://evil.example",
    ],
)
def test_app_return_url_refuses_anything_that_is_not_a_public_id(public_id: str) -> None:
    """The one interpolation into a Location header, and it is proven first."""
    with pytest.raises(ValueError):
        app_return_url(_settings(), public_id=public_id)
