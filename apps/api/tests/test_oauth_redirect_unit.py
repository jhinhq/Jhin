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

from typing import get_args

import pytest

from jhin_api.oauth.redirect import (
    CALLBACK_PATH,
    GITHUB_APP_CALLBACK_PATH,
    OAuthRedirectMisconfigured,
    OAuthReturnError,
    app_return_url,
    configured_via,
    github_app_available,
    github_app_redirect_uri,
    github_app_return_url,
    is_https_redirect,
    is_loopback_redirect,
    redirect_base,
    redirect_uri,
)
from jhin_api.settings import InsecureDeploymentError, Settings

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"


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


def test_the_return_error_vocabulary_is_closed_and_every_member_lands_on_apps() -> None:
    """Nine constants, chosen by the service, never provider text.

    Two of them — ``signed_out`` and ``expired`` — are the whole pre-claim
    tier; the other seven are reachable only past a claim. Adding a tenth
    means deciding which tier it belongs to, which is why the set is pinned
    here as well as in ``test_oauth_router_unit.py``.
    """
    assert set(get_args(OAuthReturnError)) == {
        "signed_out",
        "expired",
        "denied",
        "failed",
        "issuer_mismatch",
        "client_rejected",
        "callback_mismatch",
        "redirect_changed",
        "registration_gone",
    }
    settings = _settings(app_url="https://jhin.example.com")
    for code in get_args(OAuthReturnError):
        assert app_return_url(settings, public_id=None, error=code) == (
            f"https://jhin.example.com/apps?oauth_error={code}"
        )


def test_a_connector_type_decorates_the_landing_and_junk_is_dropped() -> None:
    """The retry button's label, proven shape-safe at the door.

    Dropped rather than raised: this whole change exists so that no callback
    ends in a 500, and a hand-edited ``connector_type`` is not worth one.
    """
    settings = _settings(app_url="https://jhin.example.com")
    assert app_return_url(settings, public_id=None, error="denied", connector_type="github") == (
        "https://jhin.example.com/apps?oauth_error=denied&app=github"
    )
    for junk in ["../evil", "GitHub", "", "a" * 51, "gi thub", "github?x=1"]:
        assert app_return_url(settings, public_id=None, error="denied", connector_type=junk) == (
            "https://jhin.example.com/apps?oauth_error=denied"
        )


def test_an_error_may_carry_the_connection_it_concerns() -> None:
    """A reconnect refusal names the connection, so the card can offer Reconnect."""
    settings = _settings(app_url="https://jhin.example.com")
    assert app_return_url(settings, public_id="a" * 32, error="failed", connector_type="mcp") == (
        f"https://jhin.example.com/apps?oauth_error=failed&connection={'a' * 32}&app=mcp"
    )


def test_a_bad_public_id_still_raises_even_beside_an_error() -> None:
    """That can only be a bug in our own code, so it stays loud."""
    with pytest.raises(ValueError):
        app_return_url(_settings(), public_id="not-hex", error="failed")


def test_the_landing_flag_is_percent_encoded() -> None:
    """It is a closed set today; the encoding is what keeps that a safe fact."""
    settings = _settings(app_url="https://jhin.example.com")
    assert "%2F" not in app_return_url(settings, public_id=None, error="denied")


def test_the_manifest_handshake_lands_on_apps_with_a_boolean_flag() -> None:
    settings = _settings(app_url="https://jhin.example.com/")
    assert github_app_return_url(settings, created=True) == (
        "https://jhin.example.com/apps?github_app=created"
    )
    assert github_app_return_url(settings, created=False) == (
        "https://jhin.example.com/apps?github_app=failed"
    )


def test_one_click_app_creation_needs_an_origin_the_outbound_policy_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest embeds the instance's own origin, so a loopback install
    is told up front instead of the manifest route answering 400 later."""
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    assert github_app_available(_settings(app_url="http://localhost:3000")) is False
    assert github_app_available(_settings(app_url="https://jhin.example.com")) is True

    monkeypatch.setenv(ALLOWLIST_ENV, "http://localhost:3000")
    assert github_app_available(_settings(app_url="http://localhost:3000")) is True

    # The redirect base is embedded too, so it has to pass on its own.
    monkeypatch.setenv(ALLOWLIST_ENV, "http://localhost:3000")
    assert (
        github_app_available(
            _settings(
                app_url="http://localhost:3000",
                oauth_redirect_base_url="http://127.0.0.1:8000",
            )
        )
        is False
    )
