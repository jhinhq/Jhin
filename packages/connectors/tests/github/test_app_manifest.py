"""One-click GitHub App provisioning: manifest, conversion, installation.

Three things are proved here. The manifest asks GitHub for exactly what the
connector's declared capabilities need and nothing wider. The conversion
registers every credential GitHub hands back with the process redactor before
it returns, and answers every failure with a sentence Jhin wrote. And the app
that comes out the other end really does mint installation tokens — the
manifest path and the existing GitHub App path are one path, not two.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.github.auth import (
    AUTH_GITHUB_APP,
    InstallationTokenCache,
    resolve_access_token,
)
from jhin_connectors.github.manifest import GITHUB_CAPABILITIES
from jhin_connectors.github.oauth import (
    GITHUB_API_ORIGIN,
    GITHUB_WEB_ORIGIN,
    MANIFEST_CONVERSION_PATH,
    GitHubAppCredentials,
    app_credentials_map,
    app_permissions,
    build_app_manifest,
    convert_app_manifest,
    installation_credentials,
    installation_url,
    manifest_post_target,
    normalize_installation_id,
    parse_app_credentials_map,
)
from jhin_connectors.github.webhook import WEBHOOK_EVENTS
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_connectors.testing.fake_github_oauth import (
    FakeGitHubOAuthConfig,
    FakeGitHubOAuthServer,
)
from jhin_oauth.errors import OAuthError
from jhin_secrets.material import decode_string_secret_map
from jhin_secrets.redaction import REDACTED, get_redactor

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
INSTANCE_ORIGIN = "https://jhin.example.com"
HOMEPAGE_URL = f"{INSTANCE_ORIGIN}/"
REDIRECT_URL = f"{INSTANCE_ORIGIN}/api/v1/oauth/github-app/callback"
CALLBACK_URL = f"{INSTANCE_ORIGIN}/api/v1/oauth/callback"
SETUP_URL = f"{INSTANCE_ORIGIN}/apps"
WEBHOOK_URL = f"{INSTANCE_ORIGIN}/api/v1/webhooks/github"


@pytest.fixture(autouse=True)
def _restore_allowlist() -> Iterator[None]:
    previous = os.environ.get(ALLOWLIST_ENV)
    # The instance's own origin stands in for a real deployment's, and being
    # allow-listed keeps these tests off the network entirely.
    _allow(INSTANCE_ORIGIN)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous


def _allow(*origins: str) -> None:
    current = [entry for entry in os.environ.get(ALLOWLIST_ENV, "").split(",") if entry]
    os.environ[ALLOWLIST_ENV] = ",".join([*current, *origins])


@contextmanager
def _fake_github(config: FakeGitHubOAuthConfig | None = None) -> Iterator[FakeGitHubOAuthServer]:
    with FakeGitHubOAuthServer(config) as server:
        _allow(server.base_url)
        yield server


def _manifest() -> dict[str, object]:
    return build_app_manifest(
        app_name="Jhin",
        homepage_url=HOMEPAGE_URL,
        redirect_url=REDIRECT_URL,
        callback_url=CALLBACK_URL,
        setup_url=SETUP_URL,
        webhook_url=WEBHOOK_URL,
    )


def _test_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


# --- The manifest -----------------------------------------------------------


def test_manifest_carries_what_github_requires() -> None:
    manifest = _manifest()
    # GitHub rejects a manifest with no `url`.
    assert manifest["url"] == HOMEPAGE_URL
    assert manifest["name"] == "Jhin"
    assert manifest["redirect_url"] == REDIRECT_URL
    assert manifest["setup_url"] == SETUP_URL
    assert manifest["request_oauth_on_install"] is True
    assert manifest["public"] is False
    assert manifest["setup_on_update"] is True


def test_manifest_registers_exactly_one_callback_url() -> None:
    """One instance, one constant redirect URI. No wildcard, no second entry."""
    assert _manifest()["callback_urls"] == [CALLBACK_URL]


def test_manifest_asks_only_for_the_permissions_the_capabilities_need() -> None:
    permissions = app_permissions()
    assert permissions == {
        "metadata": "read",
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "checks": "read",
        "actions": "write",
    }
    # Whoever installs this app hands its reach to every agent with a grant to
    # the connection, so nothing here may be administrative.
    assert "admin" not in set(permissions.values())
    assert _manifest()["default_permissions"] == permissions


def test_every_declared_capability_has_a_permission() -> None:
    # app_permissions refuses to guess, so this covers the whole list at once.
    assert app_permissions(list(GITHUB_CAPABILITIES))
    with pytest.raises(ValueError, match="no GitHub App permission"):
        app_permissions(["github.repository.delete_everything"])


def test_a_webhook_url_brings_the_events_and_no_url_brings_neither() -> None:
    with_hook = _manifest()
    assert with_hook["hook_attributes"] == {"url": WEBHOOK_URL, "active": True}
    assert with_hook["default_events"] == list(WEBHOOK_EVENTS)

    without_hook = build_app_manifest(
        app_name="Jhin",
        homepage_url=HOMEPAGE_URL,
        redirect_url=REDIRECT_URL,
        callback_url=CALLBACK_URL,
        setup_url=SETUP_URL,
        webhook_url=None,
    )
    assert "hook_attributes" not in without_hook
    assert "default_events" not in without_hook


def test_a_url_outside_the_outbound_policy_never_reaches_a_manifest() -> None:
    with pytest.raises(EndpointPolicyError):
        build_app_manifest(
            app_name="Jhin",
            homepage_url=HOMEPAGE_URL,
            redirect_url=REDIRECT_URL,
            callback_url="http://169.254.169.254/api/v1/oauth/callback",
            setup_url=SETUP_URL,
            webhook_url=None,
        )


@pytest.mark.parametrize("app_name", ["", "x" * 35, "Jhin\r\nX-Injected: 1", "-leading-dash"])
def test_an_app_name_github_would_refuse_is_refused_here(app_name: str) -> None:
    with pytest.raises(ValueError):
        build_app_manifest(
            app_name=app_name,
            homepage_url=HOMEPAGE_URL,
            redirect_url=REDIRECT_URL,
            callback_url=CALLBACK_URL,
            setup_url=SETUP_URL,
            webhook_url=None,
        )


def test_the_form_target_is_the_account_the_operator_chose() -> None:
    assert manifest_post_target(organization=None) == f"{GITHUB_WEB_ORIGIN}/settings/apps/new"
    assert manifest_post_target(organization="  ") == f"{GITHUB_WEB_ORIGIN}/settings/apps/new"
    assert (
        manifest_post_target(organization="octo-org")
        == f"{GITHUB_WEB_ORIGIN}/organizations/octo-org/settings/apps/new"
    )
    for hostile in ("octo/../../settings", "octo org", "octo?x=1", "-octo"):
        with pytest.raises(ValueError):
            manifest_post_target(organization=hostile)


# --- The conversion ---------------------------------------------------------


async def test_conversion_returns_the_app_and_redacts_every_secret() -> None:
    with _fake_github() as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            app = await convert_app_manifest(client, "fake-manifest-code")

    assert app.slug == "jhin-fake-instance"
    assert app.app_id == "424242"
    assert app.client_id.startswith("Iv1.fake")
    assert app.html_url.endswith("/apps/jhin-fake-instance")

    redactor = get_redactor()
    for secret in (app.client_secret, app.webhook_secret, app.private_key_pem):
        assert secret
        assert redactor.redact_text(f"leaked: {secret}") == "leaked: " + REDACTED


async def test_a_code_that_is_not_a_code_never_reaches_github() -> None:
    with _fake_github() as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            for hostile in ("../../repos/octo/alpha", "code with spaces", "", "a/b"):
                with pytest.raises(OAuthError, match="app-creation code"):
                    await convert_app_manifest(client, hostile)
        assert server.conversion_requests == []


async def test_a_refused_conversion_says_what_to_do_and_nothing_else() -> None:
    with _fake_github(FakeGitHubOAuthConfig(manifest_conversion_status=404)) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            with pytest.raises(OAuthError) as excinfo:
                await convert_app_manifest(client, "fake-manifest-code")
    message = str(excinfo.value)
    assert "single-use" in message
    assert "404" not in message
    assert "fake GitHub" not in message


def test_the_conversion_path_is_the_documented_one() -> None:
    assert MANIFEST_CONVERSION_PATH.format(code="abc") == "/app-manifests/abc/conversions"
    assert GITHUB_API_ORIGIN == "https://api.github.com"


# --- Installation -----------------------------------------------------------


def test_the_installation_url_carries_the_handle_and_nothing_else() -> None:
    url = installation_url("jhin-fake-instance", state="abcDEF-123_456")
    assert url == (
        f"{GITHUB_WEB_ORIGIN}/apps/jhin-fake-instance/installations/new?state=abcDEF-123_456"
    )
    for hostile_state in ("", "a b", "a&redirect_uri=https://evil.example", "x" * 257):
        with pytest.raises(ValueError):
            installation_url("jhin-fake-instance", state=hostile_state)
    for hostile_slug in ("../../settings", "Jhin Fake", "slug?x=1"):
        with pytest.raises(ValueError):
            installation_url(hostile_slug, state="handle")


def test_an_installation_id_is_a_number_or_it_is_nothing() -> None:
    assert normalize_installation_id(" 12345 ") == "12345"
    for hostile in ("", "0", "12a", "1/../2", "-1", "9" * 21):
        with pytest.raises(ValueError):
            normalize_installation_id(hostile)


def test_app_credentials_survive_the_encrypted_secret_store_shape() -> None:
    app = GitHubAppCredentials(
        app_id="424242",
        slug="jhin-fake-instance",
        client_id="Iv1.fakeclientid",
        client_secret="fake-github-client-secret",
        webhook_secret="fake-github-webhook-secret",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        html_url=f"{GITHUB_WEB_ORIGIN}/apps/jhin-fake-instance",
    )
    material = app_credentials_map(app)
    # The store only accepts a flat string -> string object.
    assert decode_string_secret_map(json.dumps(material)) == material
    assert parse_app_credentials_map(material) == app

    incomplete = dict(material)
    del incomplete["private_key"]
    with pytest.raises(ValueError, match="private_key"):
        parse_app_credentials_map(incomplete)


def test_installation_credentials_hold_the_key_and_never_the_secret() -> None:
    app = GitHubAppCredentials(
        app_id="424242",
        slug="jhin-fake-instance",
        client_id="Iv1.fakeclientid",
        client_secret="fake-github-client-secret",
        webhook_secret="fake-github-webhook-secret",
        private_key_pem="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        html_url="",
    )
    credentials = installation_credentials(app, installation_id="42")
    assert set(credentials) == {"app_id", "private_key", "installation_id"}
    # Minting is signed with the key, so the app's secret has no business here.
    assert app.client_secret not in credentials.values()
    # GitHub now recommends the client id as the JWT issuer.
    assert credentials["app_id"] == app.client_id


async def test_an_app_created_from_a_manifest_mints_installation_tokens() -> None:
    """The whole point: provisioning ends in a credential that already works."""
    with _fake_github() as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            converted = await convert_app_manifest(client, "fake-manifest-code")

    # The fake's PEM is deliberately not a key; signing needs a real one.
    app = dataclasses.replace(converted, private_key_pem=_test_private_key())
    credentials = installation_credentials(app, installation_id="42")

    with FakeGitHubServer() as api:
        _allow(api.base_url)
        token = await resolve_access_token(
            AUTH_GITHUB_APP,
            credentials,
            api.base_url,
            cache=InstallationTokenCache(),
        )
    assert token.startswith("ghs_fake_42_")
    assert get_redactor().redact_text(token) == REDACTED
