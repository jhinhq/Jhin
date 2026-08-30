"""The catalog icon proxy behind ``/api/v1/catalog/entries/{slug}/icon``.

Every catalog card whose entry ships a logo points its ``<img>`` here — the
stored upstream URL never reaches a browser (docs/architecture/catalog.md).
Serving the logo same-origin is the whole point: no third-party dial per page
view, no referrer leaking who browses the library, and one place where the
bytes are inspected before anybody renders them.

The SSRF posture is the sync's, applied twice more. The stored URL was held
to a two-shape allowlist at ingest (``jhin_catalog_sync.wire.safe_icon_url``);
:func:`jhin_api.catalog.service.icon_source_url` re-passes it on the way out,
and every redirect hop is validated against the same allowlist (plus the one
GitHub-avatar redirect target the GitHub shape is allowed to land on) before
it is followed. A URL that fails any gate is simply "no logo".

The body is trusted only as far as its magic bytes: the proxy serves the four
raster formats it can recognise from the first bytes (PNG, JPEG, WebP, GIF),
with the content type derived from that sniff and never from the upstream
header. SVG is rejected outright — there is no sanitiser here, and a script
inside a same-origin SVG is exactly the payload this route must never store.
An entry whose upstream serves something else falls down the card's own
glyph/monogram fallback chain, which is the graceful path by design.

Results are cached in ``catalog_icon`` either way: a fetched logo is stored
once and served from the database after that, and a failed fetch is cached
for a week, so a dead upstream costs one request a week instead of one per
page view.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from fastapi import HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.catalog import service
from jhin_catalog_sync.wire import ICON_URL_REDIRECT_PREFIX, safe_icon_url
from jhin_connectors.mcp.discovery import is_valid_server_slug
from jhin_db.models import CatalogIcon

#: A failed fetch is not retried before this has passed.
FAILURE_TTL: Final = timedelta(days=7)

#: A logo larger than this is not a logo.
MAX_ICON_BYTES: Final = 256 * 1024

#: The GitHub avatar shape answers with one redirect; anything needing more
#: than a couple of hops is not the URL that was reviewed.
_MAX_REDIRECTS: Final = 3

_FETCH_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

#: Belt and braces on the response: the sniffed content type is authoritative
#: (`nosniff`), and even a body that somehow renders as a document may load
#: nothing and run nothing.
_ICON_RESPONSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "private, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})


class _FetchRejected(Exception):
    """The upstream answer is not a logo this proxy will store or serve."""


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Icon not found")


def sniffed_content_type(body: bytes) -> str:
    """The content type the first bytes actually claim, or "".

    The upstream ``Content-Type`` header is never consulted: the magic bytes
    are what a browser would sniff, so they are what gets policed."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return ""


def _allowed_hop(url: str) -> bool:
    """Whether a redirect target is somewhere the proxy may follow: one of the
    two reviewed shapes, or the avatars host the GitHub shape redirects to."""
    if url.startswith(ICON_URL_REDIRECT_PREFIX):
        return "/./" not in url and "/../" not in url
    return bool(safe_icon_url(url))


async def _fetch_upstream(url: str) -> tuple[bytes, str]:
    """The upstream logo as ``(body, sniffed content type)``.

    Redirects are followed by hand so every hop passes the allowlist; the body
    is read in chunks against the byte cap, so an upstream that lies about (or
    omits) ``Content-Length`` still cannot hand over more than the cap."""
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    if not _allowed_hop(location):
                        raise _FetchRejected("redirect target off the allowlist")
                    url = location
                    continue
                if response.status_code != 200:
                    raise _FetchRejected(f"upstream answered {response.status_code}")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_ICON_BYTES:
                        raise _FetchRejected("upstream body over the byte cap")
                content_type = sniffed_content_type(body)
                if not content_type:
                    raise _FetchRejected("upstream body is not a recognised raster image")
                return body, content_type
        raise _FetchRejected("too many redirects")


def _fresh_failure(row: CatalogIcon) -> bool:
    if row.fetched_at is None:
        return False
    fetched_at = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - fetched_at < FAILURE_TTL


def _icon_response(content_type: str, body: bytes) -> Response:
    return Response(content=body, media_type=content_type, headers=_ICON_RESPONSE_HEADERS)


async def _store(
    db: AsyncSession,
    slug: str,
    *,
    source_url: str,
    content_type: str,
    body: bytes | None,
    ok: bool,
) -> None:
    """Upsert the cache row. Two viewers racing on a cold slug both fetch; the
    loser of the unique-slug insert simply keeps the winner's row."""
    row = await db.scalar(select(CatalogIcon).where(CatalogIcon.slug == slug))
    if row is None:
        row = CatalogIcon(slug=slug)
        db.add(row)
    row.source_url = source_url
    row.content_type = content_type
    row.body = body
    row.status = "ok" if ok else "failed"
    row.fetched_at = datetime.now(UTC)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()


async def serve_icon(db: AsyncSession, slug: str) -> Response:
    """The cached logo for ``slug``, fetching and caching it on first ask.

    404 everywhere the answer is "no logo": an unknown slug, an entry without
    an upstream URL, a fetch that failed, a body the sniff rejected. The card
    in front of this route treats a 404 as "fall back to the glyph"."""
    if not is_valid_server_slug(slug):
        raise _not_found()

    cached = await db.scalar(select(CatalogIcon).where(CatalogIcon.slug == slug))
    if cached is not None:
        if cached.status == "ok" and cached.body:
            return _icon_response(cached.content_type, cached.body)
        if cached.status == "failed" and _fresh_failure(cached):
            raise _not_found()

    upstream = await service.icon_source_url(db, slug)
    if not upstream:
        raise _not_found()

    try:
        body, content_type = await _fetch_upstream(upstream)
    except (_FetchRejected, httpx.HTTPError):
        await _store(db, slug, source_url=upstream, content_type="", body=None, ok=False)
        raise _not_found() from None

    await _store(db, slug, source_url=upstream, content_type=content_type, body=body, ok=True)
    return _icon_response(content_type, body)
