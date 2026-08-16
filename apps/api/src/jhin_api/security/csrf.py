"""CSRF protection for cookie-authenticated browser requests (plan 20.1).

Double-submit pattern: login issues a random token in a JavaScript-readable
cookie; the browser must echo it back in a header on every mutating request.
A cross-site attacker can force the cookie to be sent but cannot read it to
forge the header.
"""

import hmac

from fastapi import HTTPException, Request, status

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    settings = request.app.state.settings
    cookie_token: str | None = request.cookies.get(settings.csrf_cookie_name)
    header_token: str | None = request.headers.get(settings.csrf_header_name)
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )
