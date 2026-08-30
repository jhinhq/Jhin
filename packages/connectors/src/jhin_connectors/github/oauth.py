"""GitHub sign-in that never asks anyone to paste a token.

**App-manifest provisioning** (:func:`build_app_manifest`,
:func:`manifest_post_target`, :func:`convert_app_manifest`) sits on top of the
generic OAuth core in :mod:`jhin_oauth`, and it never ends with a human typing
a credential: the operator clicks once, GitHub creates this instance's own
GitHub App, and a single exchange hands back its client id, client secret,
webhook secret, and private key. Nothing is copied by hand and no secret
crosses a screen. Once the app is installed on an account, GitHub returns an
installation id (:func:`normalize_installation_id`), and that plus the app's
private key is exactly the credential
:func:`jhin_connectors.github.auth.resolve_access_token` already mints
short-lived installation tokens from; this module does not re-implement the
minting.

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

from httpx import AsyncClient

from jhin_connectors.github.client import API_VERSION, USER_AGENT
from jhin_connectors.github.manifest import GITHUB_CAPABILITIES
from jhin_connectors.github.webhook import WEBHOOK_EVENTS
from jhin_connectors.http_client import send_bounded_json
from jhin_oauth.errors import OAuthError
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

# GitHub's own naming rules, applied before any of these reach a URL or a
# form: an app name, an account login, an app slug, a one-time manifest code,
# and an installation id.
_APP_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,33}$")
_LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_SLUG_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_MANIFEST_CODE_RE: Final = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_INSTALLATION_ID_RE: Final = re.compile(r"^[1-9][0-9]{0,19}$")


# --- Messages, all of them ours ---------------------------------------------

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


# --- Validation helpers -----------------------------------------------------


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
    "app_permissions",
    "build_app_manifest",
    "convert_app_manifest",
    "manifest_post_target",
    "normalize_installation_id",
]
