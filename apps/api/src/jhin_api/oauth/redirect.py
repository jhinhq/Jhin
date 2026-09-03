"""The one redirect URI this instance uses, and the one place a browser
leaving the callback is sent (``docs/architecture/oauth.md``).

Every distinguishing fact about an authorization — which provider, which
workspace, which connection, where the user was — travels in the opaque state
handle and is looked up server-side. None of it is in the URL, because a
redirect URI that varies is a redirect URI that has to be matched loosely, and
loose matching is how open redirectors are built.

So there is exactly one callback URI per instance, derived from settings, and
these functions are the only source of it. It is recomputed here at every call
site rather than read back from a request or a row: a URI that came from a
request is a destination an attacker chose, and there is no amount of
validation that makes that a good idea.

:func:`app_return_url` is the only function in the codebase permitted to build
a ``Location`` header for a browser leaving the callback. It takes settings
and a public id, refuses a public id that is not thirty-two hex characters,
and has no parameter through which a request-supplied string could reach it.
"""

from __future__ import annotations

import re
from typing import Final, Literal
from urllib.parse import quote, urlsplit

from jhin_api.settings import InsecureDeploymentError, Settings
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_oauth.urls import validate_oauth_url

CALLBACK_PATH: Final[str] = "/api/v1/oauth/callback"
GITHUB_APP_CALLBACK_PATH: Final[str] = "/api/v1/oauth/github-app/callback"

#: ``Connection.public_id`` is ``secrets.token_hex(16)``. Anything else is not
#: one of ours and does not go into a ``Location`` header.
_PUBLIC_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")

#: The closed set of flags a browser leaving the callback can carry. Chosen
#: by the service from the provider's *machine-readable* code, never from its
#: prose; the web app turns each into a sentence Jhin wrote.
OAuthReturnError = Literal["denied", "failed", "client_rejected", "callback_mismatch"]


class OAuthRedirectMisconfigured(InsecureDeploymentError):
    """``OAUTH_REDIRECT_BASE_URL`` / ``APP_URL`` cannot host a redirect URI.

    Subclasses the deployment-refusal error the settings validator already
    raises, so a misconfiguration reaching this module late behaves like the
    startup refusal rather than like a request-time bug.
    """


def redirect_base(settings: Settings) -> str:
    """The normalized origin providers redirect back to, without a trailing slash.

    ``OAUTH_REDIRECT_BASE_URL`` when the operator set one, ``APP_URL``
    otherwise — which is right for every deployment where the browser reaches
    the API through the web app's rewrite proxy, and that is the default
    shape of a Jhin install.
    """
    configured = settings.oauth_redirect_base_url.strip()
    raw = configured or settings.app_url.strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise OAuthRedirectMisconfigured(
            "OAUTH_REDIRECT_BASE_URL (or APP_URL) must be an absolute http:// or "
            "https:// origin before any app can be connected with OAuth."
        )
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise OAuthRedirectMisconfigured(
            "OAUTH_REDIRECT_BASE_URL (or APP_URL) must be a bare origin: no query "
            "string, no fragment, and no username or password."
        )
    if parsed.path not in {"", "/"}:
        raise OAuthRedirectMisconfigured(
            "OAUTH_REDIRECT_BASE_URL (or APP_URL) must be a bare origin with no path."
        )
    authority = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{scheme}://{authority}"


def redirect_uri(settings: Settings) -> str:
    """The callback URI Jhin registers with every provider, verbatim.

    One constant string. Providers match it exactly; Jhin compares it
    byte-for-byte at callback time against what a pending authorization
    recorded, so an operator who changes the base URL mid-flow gets a refusal
    rather than a token bound to a URI nobody registered.
    """
    return f"{redirect_base(settings)}{CALLBACK_PATH}"


def github_app_redirect_uri(settings: Settings) -> str:
    """Where GitHub sends the browser after creating an app from a manifest."""
    return f"{redirect_base(settings)}{GITHUB_APP_CALLBACK_PATH}"


def configured_via(settings: Settings) -> Literal["OAUTH_REDIRECT_BASE_URL", "APP_URL"]:
    """Which setting produced the redirect URI, for the operator-facing screen."""
    return "OAUTH_REDIRECT_BASE_URL" if settings.oauth_redirect_base_url.strip() else "APP_URL"


def is_loopback_redirect(settings: Settings) -> bool:
    """Whether the redirect URI points at this machine.

    A loopback redirect URI is legitimate for a laptop install and useless to
    a provider that has to reach it, so the web app says so plainly instead of
    letting somebody discover it at the consent screen.
    """
    host = (urlsplit(redirect_base(settings)).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost")


def is_https_redirect(settings: Settings) -> bool:
    return redirect_base(settings).startswith("https://")


def app_return_url(
    settings: Settings,
    *,
    public_id: str | None,
    error: OAuthReturnError | None = None,
) -> str:
    """Where the browser goes after the callback. The only such builder.

    Built from settings plus, at most, one connection public id that has been
    proven to be thirty-two hex characters. Nothing a request supplied — no
    ``next``, no ``redirect_uri``, no ``state`` payload — can reach this
    function, which is what closes the open-redirect surface by construction
    rather than by validation.
    """
    base = settings.app_url.strip().rstrip("/")
    if error is not None:
        return f"{base}/apps?oauth_error={quote(error, safe='')}"
    if public_id is None:
        return f"{base}/apps"
    if not _PUBLIC_ID_RE.fullmatch(public_id):
        raise ValueError("connection public id is not a 32-character hex token")
    return f"{base}/apps?connection={public_id}"


def github_app_return_url(settings: Settings, *, created: bool) -> str:
    """Where the browser goes after the GitHub App manifest handshake.

    Apps, not Settings: the app was created so that GitHub could be
    connected, and the Apps page reads the flag and opens Connect GitHub. A
    boolean is the only input, so nothing a request carried can reach the
    ``Location`` header this becomes.
    """
    base = settings.app_url.strip().rstrip("/")
    return f"{base}/apps?github_app={'created' if created else 'failed'}"


def github_app_available(settings: Settings) -> bool:
    """Whether a GitHub App manifest can be built for this instance at all.

    The manifest embeds two of this instance's own origins — ``APP_URL`` as
    the homepage and setup page, the redirect base as the callback — and
    every URL in a manifest goes through the outbound policy on the way out.
    A loopback or plain-HTTP origin that ``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS``
    does not list is refused there, so the web app is told up front and
    offers the by-hand registration instead of a card that answers 400.
    """
    try:
        validate_oauth_url(settings.app_url.strip().rstrip("/"), kind="GitHub App homepage URL")
        validate_oauth_url(redirect_base(settings), kind="GitHub App callback URL")
    except (EndpointPolicyError, OAuthRedirectMisconfigured, ValueError):
        return False
    return True
