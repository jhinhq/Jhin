"""GitHub sign-in that never asks anyone to paste a token.

Three GitHub-shaped paths sit on top of the generic OAuth core in
:mod:`jhin_oauth`, and none of them ends with a human typing a credential:

- **Device flow** (:func:`start_github_device_authorization`,
  :func:`poll_github_device_token`) — a client id and nothing else. No
  redirect URI, no client secret, at start, at poll, or at refresh. That is
  precisely why it is the answer for an instance GitHub cannot redirect a
  browser back to: localhost, a private network, or anything without TLS.
  GitHub answers a poll that is not ready yet with **HTTP 200** and the error
  in the body, so the status code alone never decides anything here.
- **App-manifest provisioning** (:func:`build_app_manifest`,
  :func:`manifest_post_target`, :func:`convert_app_manifest`) — the operator
  clicks once, GitHub creates this instance's own GitHub App, and a single
  exchange hands back its client id, client secret, webhook secret, and
  private key. Nothing is copied by hand and no secret crosses a screen.
- **Installation** (:func:`installation_url`, :func:`installation_credentials`)
  — the app is installed on an account, GitHub returns an installation id,
  and that plus the app's private key is exactly the credential
  :func:`jhin_connectors.github.auth.resolve_access_token` already mints
  short-lived installation tokens from. This module hands that path its
  credential map; it does not re-implement the minting.

Two rules hold throughout. Every credential is registered with the process
redactor at the moment of first possession, so it cannot survive into a log
line. And every message raised from here is a sentence Jhin wrote: GitHub's
``error_description`` is attacker-influenced text and is never rendered,
returned, or embedded in an exception.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

from httpx import AsyncClient

from jhin_connectors.github.client import API_VERSION, USER_AGENT
from jhin_connectors.github.manifest import GITHUB_CAPABILITIES
from jhin_connectors.github.webhook import WEBHOOK_EVENTS
from jhin_connectors.http_client import send_bounded_json
from jhin_oauth.errors import OAuthError, TokenError
from jhin_oauth.tokens import poll_device_token, start_device_authorization
from jhin_oauth.types import DeviceCodeGrant, DeviceTokenPending, TokenResponse
from jhin_oauth.urls import validate_oauth_url
from jhin_secrets.redaction import get_redactor

# --- Where GitHub lives -----------------------------------------------------

GITHUB_WEB_ORIGIN: Final[str] = "https://github.com"
GITHUB_API_ORIGIN: Final[str] = "https://api.github.com"

#: The issuer OAuth client registrations for GitHub are keyed by. GitHub
#: publishes no RFC 8414 metadata, so this is the identity Jhin assigns it and
#: must stay byte-identical wherever a GitHub registration is looked up.
GITHUB_ISSUER: Final[str] = "https://github.com"

AUTHORIZE_URL: Final[str] = "https://github.com/login/oauth/authorize"
DEVICE_CODE_URL: Final[str] = "https://github.com/login/device/code"
DEVICE_TOKEN_URL: Final[str] = "https://github.com/login/oauth/access_token"
MANIFEST_CONVERSION_PATH: Final[str] = "/app-manifests/{code}/conversions"

# --- Bounds on everything GitHub sends back ---------------------------------

MAX_CONVERSION_RESPONSE_BYTES: Final[int] = 65_536
MAX_IDENTIFIER_CHARS: Final[int] = 500
MAX_SECRET_CHARS: Final[int] = 4_096
MAX_PRIVATE_KEY_CHARS: Final[int] = 16_384
#: GitHub's own limit on a GitHub App name.
MAX_APP_NAME_CHARS: Final[int] = 34

# --- Keys in the stored app-credential map ----------------------------------

APP_ID_KEY: Final[str] = "app_id"
SLUG_KEY: Final[str] = "slug"
CLIENT_ID_KEY: Final[str] = "client_id"
CLIENT_SECRET_KEY: Final[str] = "client_secret"
WEBHOOK_SECRET_KEY: Final[str] = "webhook_secret"
PRIVATE_KEY_KEY: Final[str] = "private_key"
HTML_URL_KEY: Final[str] = "html_url"
INSTALLATION_ID_KEY: Final[str] = "installation_id"

_REQUIRED_APP_KEYS: Final[tuple[str, ...]] = (
    APP_ID_KEY,
    SLUG_KEY,
    CLIENT_ID_KEY,
    CLIENT_SECRET_KEY,
    PRIVATE_KEY_KEY,
)

# GitHub's own naming rules, applied before any of these reach a URL or a
# form: an app name, an account login, an app slug, a one-time manifest code,
# an opaque state handle, an installation id, and a client id.
_APP_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,33}$")
_LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_SLUG_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_MANIFEST_CODE_RE: Final = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_STATE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_INSTALLATION_ID_RE: Final = re.compile(r"^[1-9][0-9]{0,19}$")
_CLIENT_ID_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# --- Messages, all of them ours ---------------------------------------------

_DEVICE_FLOW_MESSAGES: Final[Mapping[str, str]] = {
    "device_flow_disabled": (
        "This GitHub app has device flow switched off. An admin needs to tick "
        "'Enable Device Flow' in the app's settings on GitHub, then try again."
    ),
    "incorrect_client_credentials": (
        "GitHub does not recognise this app's client ID. Check it in Settings, then try again."
    ),
    "incorrect_device_code": (
        "GitHub no longer recognises this sign-in attempt. Start again from Apps."
    ),
    "unsupported_grant_type": (
        "GitHub refused a device sign-in for this app. Create the app again "
        "with device flow enabled, or connect GitHub in the browser instead."
    ),
    "expired_token": "That code expired before it was approved. Start again to get a new one.",
    "access_denied": "The sign-in was declined on GitHub.",
    "invalid_request": "GitHub rejected this sign-in request. Start again from Apps.",
    "invalid_client": (
        "GitHub does not recognise this app's client ID. Check it in Settings, then try again."
    ),
}
_DEFAULT_DEVICE_FLOW_MESSAGE: Final[str] = (
    "GitHub could not finish this sign-in. Start again from Apps."
)

_CONVERSION_FAILED_MESSAGE: Final[str] = (
    "GitHub did not accept this app-creation code. Each code is single-use and "
    "expires an hour after the form is submitted, so start again from Settings."
)
_CONVERSION_SHAPE_MESSAGE: Final[str] = (
    "GitHub's reply to the app-creation request was not the app credentials Jhin expected."
)
_BAD_MANIFEST_CODE_MESSAGE: Final[str] = "That is not a GitHub app-creation code."

# The narrowest GitHub App permission each declared capability needs. Adding a
# capability to the GitHub connector without adding it here is a build-time
# error, not a silently under-permissioned app: see :func:`app_permissions`.
_CAPABILITY_PERMISSIONS: Final[Mapping[str, tuple[str, str]]] = {
    "github.repository.read": ("contents", "read"),
    "github.branch.create": ("contents", "write"),
    "github.issue.read": ("issues", "read"),
    "github.issue.comment": ("issues", "write"),
    "github.pull_request.create": ("pull_requests", "write"),
    "github.pull_request.read": ("pull_requests", "read"),
    "github.pull_request.comment": ("pull_requests", "write"),
    "github.pull_request.merge": ("pull_requests", "write"),
    "github.check.read": ("checks", "read"),
    "github.workflow.dispatch": ("actions", "write"),
    "github.workflow_run.read": ("actions", "read"),
}
_ACCESS_RANK: Final[Mapping[str, int]] = {"read": 1, "write": 2, "admin": 3}


def device_flow_message(error_code: str) -> str:
    """The operator-facing sentence for one GitHub device-flow error code.

    Constant text only. GitHub's ``error_description`` is attacker-influenced
    prose and never reaches a user, a log, or an exception; the machine
    readable code — already narrowed to a known value by
    :mod:`jhin_oauth.errors` — chooses one of Jhin's own sentences instead.
    """
    return _DEVICE_FLOW_MESSAGES.get(error_code, _DEFAULT_DEVICE_FLOW_MESSAGE)


# --- Device flow ------------------------------------------------------------


async def start_github_device_authorization(
    client: AsyncClient,
    *,
    client_id: str,
    scope: str = "",
    device_authorization_endpoint: str = DEVICE_CODE_URL,
) -> DeviceCodeGrant:
    """Ask GitHub for a user code, the first half of the device flow.

    Only the client id is sent. GitHub's device flow has no client secret and
    no redirect URI at any step, which is the whole reason this is the flow
    for an instance a provider cannot reach.

    ``device_authorization_endpoint`` moves for GitHub Enterprise Server (and
    for the in-process fake); it is put through Jhin's outbound URL policy
    like any other endpoint.

    Raises :class:`~jhin_oauth.errors.TokenError`,
    :class:`~jhin_oauth.errors.TransientOAuthError`, and
    :class:`~jhin_connectors.endpoints.EndpointPolicyError`.
    """
    endpoint = validate_oauth_url(
        device_authorization_endpoint, kind="GitHub device authorization endpoint"
    )
    try:
        return await start_device_authorization(
            client,
            device_authorization_endpoint=endpoint,
            client_id=_validated_client_id(client_id),
            scope=scope,
        )
    except TokenError as exc:
        raise TokenError(device_flow_message(exc.error_code), error_code=exc.error_code) from None


async def poll_github_device_token(
    client: AsyncClient,
    *,
    client_id: str,
    device_code: str,
    token_endpoint: str = DEVICE_TOKEN_URL,
) -> TokenResponse | DeviceTokenPending:
    """Ask once whether the person has approved the device code yet.

    There is deliberately no ``client_secret`` parameter: GitHub authenticates
    a device grant by client id alone, and an instance on this path may well
    have no secret to give. A :class:`~jhin_oauth.types.DeviceTokenPending`
    result is not a failure — feed it through
    :func:`jhin_oauth.tokens.next_poll_interval`, which is where the rule that
    ``slow_down`` raises the cadence permanently lives, and ask again.

    Raises :class:`~jhin_oauth.errors.DeviceAuthorizationDenied` when the
    person declined, :class:`~jhin_oauth.errors.DeviceCodeExpired` when the
    code timed out, :class:`~jhin_oauth.errors.TokenError` for anything else,
    and :class:`~jhin_connectors.endpoints.EndpointPolicyError` for an
    endpoint policy refuses.
    """
    endpoint = validate_oauth_url(token_endpoint, kind="GitHub token endpoint")
    try:
        return await poll_device_token(
            client,
            token_endpoint=endpoint,
            client_id=_validated_client_id(client_id),
            device_code=device_code,
        )
    except TokenError as exc:
        raise TokenError(device_flow_message(exc.error_code), error_code=exc.error_code) from None


# --- App-manifest provisioning ----------------------------------------------


def app_permissions(capabilities: Sequence[str] = GITHUB_CAPABILITIES) -> dict[str, str]:
    """The narrowest GitHub App permissions that still run every capability.

    Scope minimisation is a requirement, not a preference: whoever installs
    this app hands its permissions to every agent holding a grant to the
    connection. The map is derived from the connector's declared capabilities
    so it cannot drift wider than the tools that exist.

    ``metadata: read`` is included because GitHub grants it to every app
    regardless; naming it keeps the manifest honest about what is asked for.

    Raises :class:`ValueError` for a capability with no permission mapped to
    it — an under-permissioned app that fails hours later in an agent's hands
    is worse than a loud failure here.
    """
    permissions: dict[str, str] = {"metadata": "read"}
    for capability in capabilities:
        entry = _CAPABILITY_PERMISSIONS.get(capability)
        if entry is None:
            raise ValueError(f"no GitHub App permission is mapped for capability {capability!r}")
        name, access = entry
        if _ACCESS_RANK[access] > _ACCESS_RANK.get(permissions.get(name, ""), 0):
            permissions[name] = access
    return permissions


def build_app_manifest(
    *,
    app_name: str,
    homepage_url: str,
    redirect_url: str,
    callback_url: str,
    setup_url: str,
    webhook_url: str | None,
) -> dict[str, Any]:
    """The manifest document the operator's browser POSTs to GitHub.

    GitHub creates the app from this and hands back a one-time code for
    :func:`convert_app_manifest`. Four URLs, each with a different job:

    - ``url`` is the app's homepage. GitHub **requires** it.
    - ``redirect_url`` is where GitHub sends the operator straight after
      creating the app, carrying the conversion code.
    - ``callback_urls`` holds exactly one entry — this instance's single
      constant OAuth redirect URI. No wildcard, no second entry, nothing
      derived from a request.
    - ``setup_url`` is where GitHub sends the operator after they install the
      app; it carries the ``installation_id`` that installation tokens are
      minted for.

    Raises :class:`ValueError` for an unusable app name and
    :class:`~jhin_connectors.endpoints.EndpointPolicyError` for a URL Jhin's
    outbound policy refuses.
    """
    manifest: dict[str, Any] = {
        "name": _validated_app_name(app_name),
        "url": validate_oauth_url(homepage_url, kind="GitHub App homepage URL"),
        "redirect_url": validate_oauth_url(redirect_url, kind="GitHub App manifest redirect URL"),
        "callback_urls": [validate_oauth_url(callback_url, kind="GitHub App callback URL")],
        "setup_url": validate_oauth_url(setup_url, kind="GitHub App setup URL"),
        # Send the operator back through setup when the installation changes,
        # so a re-scoped installation refreshes its id instead of going stale.
        "setup_on_update": True,
        # Nobody else's account should be able to install this instance's app.
        "public": False,
        # The install itself authorizes the user, so one visit to GitHub does
        # both halves and the operator never comes back for a second consent.
        "request_oauth_on_install": True,
        "default_permissions": app_permissions(),
    }
    if webhook_url is not None:
        manifest["hook_attributes"] = {
            "url": validate_oauth_url(webhook_url, kind="GitHub App webhook URL"),
            "active": True,
        }
        manifest["default_events"] = list(WEBHOOK_EVENTS)
    return manifest


def manifest_post_target(*, organization: str | None) -> str:
    """Where the manifest form is POSTed: a personal account or an org.

    Raises :class:`ValueError` for something that is not a GitHub login. The
    rejected value is never interpolated into the message.
    """
    if organization is None or not organization.strip():
        return f"{GITHUB_WEB_ORIGIN}/settings/apps/new"
    login = organization.strip()
    if not _LOGIN_RE.fullmatch(login):
        raise ValueError("that is not a GitHub organization name")
    return f"{GITHUB_WEB_ORIGIN}/organizations/{login}/settings/apps/new"


@dataclass(frozen=True, slots=True)
class GitHubAppCredentials:
    """One GitHub App this instance owns, exactly as GitHub handed it over."""

    app_id: str
    slug: str
    client_id: str
    client_secret: str
    webhook_secret: str
    private_key_pem: str
    html_url: str


async def convert_app_manifest(client: AsyncClient, code: str) -> GitHubAppCredentials:
    """Trade a one-time manifest code for this instance's own GitHub App.

    The exchange is unauthenticated by design: the code *is* the credential,
    it is single-use, and GitHub expires it an hour after the operator posted
    the form. The whole sequence — form POST, redirect, conversion — has to
    finish inside that hour.

    The request goes to ``client``'s configured ``base_url`` when it has one,
    which is how GitHub Enterprise Server and the in-process fake are reached,
    and to api.github.com otherwise. Every returned secret is registered with
    the process redactor before this function returns.

    Raises :class:`~jhin_oauth.errors.OAuthError` — with one of Jhin's own
    sentences, never GitHub's — and
    :class:`~jhin_connectors.endpoints.EndpointPolicyError`.
    """
    conversion_code = code.strip()
    if not _MANIFEST_CODE_RE.fullmatch(conversion_code):
        raise OAuthError(_BAD_MANIFEST_CODE_MESSAGE)
    base = str(client.base_url).rstrip("/") or GITHUB_API_ORIGIN
    url = validate_oauth_url(
        base + MANIFEST_CONVERSION_PATH.format(code=conversion_code),
        kind="GitHub App manifest conversion URL",
    )
    request = client.build_request(
        "POST",
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        payload = await send_bounded_json(
            client,
            request,
            max_response_bytes=MAX_CONVERSION_RESPONSE_BYTES,
            expected_status_codes=(200, 201),
        )
    except Exception:
        # Deliberately one sentence for every failure. send_bounded_json raises
        # only ProviderHTTPError, but a transport error and a refused code are
        # the same thing to the operator: the code is spent, start again.
        raise OAuthError(_CONVERSION_FAILED_MESSAGE) from None
    if not isinstance(payload, dict):
        raise OAuthError(_CONVERSION_SHAPE_MESSAGE)

    client_secret = _required_secret(payload, "client_secret", limit=MAX_SECRET_CHARS)
    private_key_pem = _required_secret(payload, "pem", limit=MAX_PRIVATE_KEY_CHARS)
    webhook_secret = _optional_secret(payload, "webhook_secret", limit=MAX_SECRET_CHARS)
    # Registered before anything else can fail, so a later shape error still
    # cannot leave these values unredactable.
    redactor = get_redactor()
    for material in (client_secret, private_key_pem, webhook_secret):
        if material:
            redactor.register(material)

    slug = _validated_slug(_required_identifier(payload, "slug"))
    html_url = _optional_identifier(payload, "html_url")
    return GitHubAppCredentials(
        app_id=_required_identifier(payload, "id"),
        slug=slug,
        client_id=_required_identifier(payload, "client_id"),
        client_secret=client_secret,
        webhook_secret=webhook_secret,
        private_key_pem=private_key_pem,
        html_url=(
            validate_oauth_url(html_url, kind="GitHub App page URL")
            if html_url
            else f"{GITHUB_WEB_ORIGIN}/apps/{slug}"
        ),
    )


# --- Installation -----------------------------------------------------------


def installation_url(slug: str, *, state: str) -> str:
    """Where to send the operator to install the app on an account.

    ``state`` is the opaque pending-authorization handle; GitHub returns it to
    the setup URL alongside the installation id, which is what binds the
    installation to the request that started it.

    Raises :class:`ValueError` for a slug or handle that is not one.
    """
    handle = state.strip()
    if not _STATE_RE.fullmatch(handle):
        raise ValueError("that is not a pending-authorization handle")
    query = urlencode({"state": handle})
    return f"{GITHUB_WEB_ORIGIN}/apps/{_validated_slug(slug)}/installations/new?{query}"


def normalize_installation_id(raw: str) -> str:
    """The installation id from GitHub's setup redirect, proved to be one.

    It arrives as a query parameter and ends up in the path of a token
    request, so it is checked against GitHub's shape — a positive integer —
    before it is trusted anywhere. Raises :class:`ValueError`.
    """
    candidate = raw.strip()
    if not _INSTALLATION_ID_RE.fullmatch(candidate):
        raise ValueError("that is not a GitHub installation id")
    return candidate


def app_credentials_map(credentials: GitHubAppCredentials) -> dict[str, str]:
    """The flat ``str -> str`` map the encrypted secret store persists.

    Flat and stringly-typed because that is what
    :func:`~jhin_secrets.material.decode_string_secret_map` accepts, and
    because :func:`~jhin_secrets.material.register_secret_material` walks
    exactly these leaves into the redactor every time it is decrypted.
    """
    return {
        APP_ID_KEY: credentials.app_id,
        SLUG_KEY: credentials.slug,
        CLIENT_ID_KEY: credentials.client_id,
        CLIENT_SECRET_KEY: credentials.client_secret,
        WEBHOOK_SECRET_KEY: credentials.webhook_secret,
        PRIVATE_KEY_KEY: credentials.private_key_pem,
        HTML_URL_KEY: credentials.html_url,
    }


def parse_app_credentials_map(material: Mapping[str, str]) -> GitHubAppCredentials:
    """Rebuild the app from its stored secret map.

    Raises :class:`ValueError` naming the missing keys and nothing else — a
    stored value never appears in the message.
    """
    missing = [key for key in _REQUIRED_APP_KEYS if not str(material.get(key, "")).strip()]
    if missing:
        raise ValueError(f"stored GitHub App credential is missing: {', '.join(missing)}")
    return GitHubAppCredentials(
        app_id=str(material[APP_ID_KEY]),
        slug=str(material[SLUG_KEY]),
        client_id=str(material[CLIENT_ID_KEY]),
        client_secret=str(material[CLIENT_SECRET_KEY]),
        webhook_secret=str(material.get(WEBHOOK_SECRET_KEY, "")),
        private_key_pem=str(material[PRIVATE_KEY_KEY]),
        html_url=str(material.get(HTML_URL_KEY, "")),
    )


def installation_credentials(
    credentials: GitHubAppCredentials, *, installation_id: str
) -> dict[str, str]:
    """The connection credential for one installation of this instance's app.

    Exactly the three fields
    :func:`jhin_connectors.github.auth.resolve_access_token` needs to mint a
    short-lived installation token: nothing here is a long-lived API key, and
    the app's client secret is deliberately absent because minting is signed
    with the private key, not authenticated with the secret.

    ``app_id`` carries the app's **client id**, which is what GitHub now
    recommends as the JWT ``iss``; the numeric app id remains valid there and
    both shapes already flow through :func:`~jhin_connectors.github.auth.build_app_jwt`.

    Raises :class:`ValueError` for an installation id that is not one.
    """
    return {
        APP_ID_KEY: credentials.client_id,
        PRIVATE_KEY_KEY: credentials.private_key_pem,
        INSTALLATION_ID_KEY: normalize_installation_id(installation_id),
    }


# --- Validation helpers -----------------------------------------------------


def _validated_client_id(client_id: str) -> str:
    candidate = client_id.strip()
    if not _CLIENT_ID_RE.fullmatch(candidate):
        raise ValueError("that is not a GitHub client ID")
    return candidate


def _validated_app_name(app_name: str) -> str:
    candidate = app_name.strip()
    if len(candidate) > MAX_APP_NAME_CHARS or not _APP_NAME_RE.fullmatch(candidate):
        raise ValueError("that is not a usable GitHub App name")
    return candidate


def _validated_slug(slug: str) -> str:
    candidate = slug.strip()
    if not _SLUG_RE.fullmatch(candidate):
        raise ValueError("that is not a GitHub App slug")
    return candidate


def _required_identifier(payload: Mapping[str, Any], key: str) -> str:
    """One non-secret field of GitHub's reply, bounded and stringified.

    GitHub sends the app's ``id`` as a JSON number and everything else as a
    string; both land here as a stripped string.
    """
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise OAuthError(_CONVERSION_SHAPE_MESSAGE)
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_IDENTIFIER_CHARS:
        raise OAuthError(_CONVERSION_SHAPE_MESSAGE)
    return candidate


def _optional_identifier(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if candidate and len(candidate) <= MAX_IDENTIFIER_CHARS else ""


def _required_secret(payload: Mapping[str, Any], key: str, *, limit: int) -> str:
    """One secret field, kept byte-for-byte — no stripping, ever.

    A PEM's trailing newline and a secret's exact bytes are load-bearing;
    tidying them here would produce a credential that no longer works.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise OAuthError(_CONVERSION_SHAPE_MESSAGE)
    return value


def _optional_secret(payload: Mapping[str, Any], key: str, *, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > limit:
        return ""
    return value


__all__ = [
    "APP_ID_KEY",
    "AUTHORIZE_URL",
    "CLIENT_ID_KEY",
    "CLIENT_SECRET_KEY",
    "DEVICE_CODE_URL",
    "DEVICE_TOKEN_URL",
    "GITHUB_API_ORIGIN",
    "GITHUB_ISSUER",
    "GITHUB_WEB_ORIGIN",
    "HTML_URL_KEY",
    "INSTALLATION_ID_KEY",
    "MANIFEST_CONVERSION_PATH",
    "MAX_APP_NAME_CHARS",
    "PRIVATE_KEY_KEY",
    "SLUG_KEY",
    "WEBHOOK_SECRET_KEY",
    "GitHubAppCredentials",
    "app_credentials_map",
    "app_permissions",
    "build_app_manifest",
    "convert_app_manifest",
    "device_flow_message",
    "installation_credentials",
    "installation_url",
    "manifest_post_target",
    "normalize_installation_id",
    "parse_app_credentials_map",
    "poll_github_device_token",
    "start_github_device_authorization",
]
