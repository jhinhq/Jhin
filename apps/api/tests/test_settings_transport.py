"""A misconfigured production deployment must fail loudly, not serve insecurely."""

import pytest

from jhin_api.settings import InsecureDeploymentError, Settings


def test_https_production_without_cookie_secure_refuses_to_start() -> None:
    """The exact shape of the base compose.yaml default: APP_ENV=production
    with COOKIE_SECURE unset. Silently serving a session cookie without the
    Secure flag over a public origin is the failure this guards."""
    with pytest.raises(InsecureDeploymentError, match="COOKIE_SECURE"):
        Settings(app_env="production", app_url="https://jhin.example.com", cookie_secure=False)


def test_https_production_with_cookie_secure_is_accepted() -> None:
    settings = Settings(
        app_env="production", app_url="https://jhin.example.com", cookie_secure=True
    )
    assert settings.cookie_secure
    assert settings.emit_hsts


def test_plaintext_public_origin_in_production_refuses_to_start() -> None:
    with pytest.raises(InsecureDeploymentError, match="plaintext"):
        Settings(app_env="production", app_url="http://jhin.example.com")


def test_plaintext_localhost_quick_start_still_works() -> None:
    """The documented single-machine quick start must keep booting."""
    settings = Settings(app_env="production", app_url="http://localhost:3000")
    assert not settings.cookie_secure
    assert not settings.emit_hsts


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "jhin.localhost"])
def test_loopback_hosts_are_allowed_over_http(host: str) -> None:
    Settings(app_env="production", app_url=f"http://{host}:3000")


def test_development_is_never_blocked() -> None:
    settings = Settings(app_env="dev", app_url="http://anything.example.com")
    assert not settings.emit_hsts
    assert settings.expose_api_docs


def test_api_docs_are_hidden_in_production() -> None:
    settings = Settings(app_env="production", app_url="http://localhost:3000")
    assert not settings.expose_api_docs


def test_samesite_none_requires_secure() -> None:
    with pytest.raises(InsecureDeploymentError, match="SameSite=None"):
        Settings(app_env="dev", session_cookie_samesite="none", cookie_secure=False)


def test_samesite_must_be_a_real_value() -> None:
    with pytest.raises(ValueError, match="SESSION_COOKIE_SAMESITE"):
        Settings(session_cookie_samesite="banana")


def test_idle_timeout_longer_than_absolute_lifetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="SESSION_IDLE_TIMEOUT_HOURS"):
        Settings(session_ttl_hours=24, session_idle_timeout_hours=48)


def test_hsts_is_not_emitted_for_a_plaintext_production_install() -> None:
    assert not Settings(app_env="production", app_url="http://localhost:3000").emit_hsts


# --- trusted proxy parsing -------------------------------------------------


def test_no_trusted_proxies_by_default() -> None:
    assert Settings().trusted_proxy_networks == ()


def test_trusted_proxy_cidrs_are_parsed() -> None:
    settings = Settings(trusted_proxy_cidrs="10.0.0.0/8, 172.16.0.0/12 ,")
    rendered = [str(network) for network in settings.trusted_proxy_networks]
    assert rendered == ["10.0.0.0/8", "172.16.0.0/12"]


def test_unparseable_cidrs_are_dropped_rather_than_crashing_boot() -> None:
    settings = Settings(trusted_proxy_cidrs="not-a-cidr,10.0.0.0/8")
    assert [str(n) for n in settings.trusted_proxy_networks] == ["10.0.0.0/8"]
