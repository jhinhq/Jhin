"""CSRF protection for cookie-authenticated browser requests (plan 20.1).

Session-bound double submit. Login issues a CSRF token in a JavaScript-readable
cookie; the browser must echo it back in a header on every mutating request,
and the token must also verify as derived from the caller's session cookie
(see ``security.tokens``).

Two independent barriers have to fall for a forgery to work:

* the header — a cross-site request cannot set a custom header without a CORS
  preflight the API refuses;
* the session binding — planting a known value in the CSRF cookie no longer
  helps, because the value has to be the HMAC of the victim's session token.

Requests that carry no session cookie fall back to plain double submit; every
mutating route also requires authentication, so those are rejected downstream.
"""

import hmac

from fastapi import HTTPException, Request, status

from jhin_api.access.keys import bearer_token as _api_key_bearer_token
from jhin_api.security.tokens import csrf_token_matches_session

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _rejected() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF token missing or invalid",
    )


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    settings = request.app.state.settings
    # API keys are exempt, but only when the request is *purely* bearer-authed.
    # CSRF exists because browsers attach cookies automatically; a bearer header
    # is never attached automatically, so there is nothing to forge. The
    # `not session_cookie` guard is the load-bearing half: if a cookie session
    # is also present the request is still a browser request, and adding an
    # Authorization header must not become a way to skip the check.
    if (
        not request.cookies.get(settings.session_cookie_name)
        and _api_key_bearer_token(request.headers.get("authorization")) is not None
    ):
        return
    cookie_token: str | None = request.cookies.get(settings.csrf_cookie_name)
    header_token: str | None = request.headers.get(settings.csrf_header_name)
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise _rejected()

    session_token: str | None = request.cookies.get(settings.session_cookie_name)
    if session_token and not csrf_token_matches_session(cookie_token, session_token):
        # Stale token from a previous session, or one planted by an attacker.
        # `GET /api/v1/auth/me` re-issues the bound cookie, so a legitimate
        # client self-heals on its next page load.
        raise _rejected()
