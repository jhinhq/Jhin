"""Baseline security response headers for every API response.

The API only ever returns JSON and media bytes, so it can afford a genuinely
strict policy: ``default-src 'none'`` plus ``frame-ancestors 'none'`` means a
response that somehow gets rendered as a document loads nothing and cannot be
framed. The browser-facing policy for the Next.js app lives in
``apps/web/next.config.ts``; the two are independent on purpose, because the
API may be reached directly (loopback administration) without the web app.

HSTS is emitted only for deployments that declare themselves HTTPS
(``APP_ENV`` staging/production with ``COOKIE_SECURE=true``): sending it from a
plaintext quick-start install would be ignored by browsers today but is exactly
the sort of header that strands an operator later.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Interactive docs are the only HTML the API serves and they legitimately need
# their own script/style origins. They are disabled outside development.
_CSP_EXEMPT_PREFIXES = ("/docs", "/redoc", "/openapi.json")

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

HSTS_VALUE = "max-age=63072000; includeSubDomains"

PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), publickey-credentials-get=(), "
    "screen-wake-lock=(), usb=(), xr-spatial-tracking=()"
)

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", PERMISSIONS_POLICY.encode("ascii")),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware that stamps baseline security headers."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        csp_exempt = path.startswith(_CSP_EXEMPT_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
            present = {name.lower() for name, _ in headers}
            for name, value in _STATIC_HEADERS:
                if name not in present:
                    headers.append((name, value))
            if not csp_exempt and b"content-security-policy" not in present:
                headers.append((b"content-security-policy", API_CSP.encode("ascii")))
            if self.hsts and b"strict-transport-security" not in present:
                headers.append((b"strict-transport-security", HSTS_VALUE.encode("ascii")))
            # Authenticated API payloads must not linger in shared caches.
            # Routes that deliberately cache (media) set their own value first.
            if b"cache-control" not in present:
                headers.append((b"cache-control", b"no-store"))
            outgoing = dict(message)
            outgoing["headers"] = headers
            await send(outgoing)

        await self.app(scope, receive, send_with_headers)
