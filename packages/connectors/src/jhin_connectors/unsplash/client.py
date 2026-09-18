"""Fixed-origin Unsplash requests and validated hotlink/credit metadata."""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from jhin_secrets.redaction import get_redactor
from jhin_tools.errors import ToolExecutionError

API_ORIGIN = "https://api.unsplash.com"
_PATH = re.compile(r"^/(?:search/photos|photos/[A-Za-z0-9_-]+(?:/download)?)$")


class UnsplashError(ToolExecutionError):
    def __init__(self, message: str, *, code: str = "unsplash_invalid", retry_after: str = ""):
        super().__init__(
            message,
            code=code,
            hint=message,
            side_effect_possible=code == "unsplash_tracking_uncertain",
        )
        self.retry_after = retry_after


def safe_url(value: Any, host: str) -> str:
    if not isinstance(value, str) or len(value) > 3000:
        raise UnsplashError("Unsplash returned an invalid resource URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(c in value for c in ("\\", "\r", "\n", "\t"))
    ):
        raise UnsplashError("Unsplash returned a resource outside its approved host")
    return value


def referral(value: Any) -> str:
    parsed = urlsplit(safe_url(value, "unsplash.com"))
    query = dict(parse_qsl(parsed.query))
    query.update(utm_source="jhin", utm_medium="referral")
    return urlunsplit(parsed._replace(query=urlencode(query)))


def photo_metadata(photo: dict[str, Any]) -> dict[str, Any]:
    try:
        identifier = str(photo["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            raise ValueError
        image = safe_url(photo["urls"]["regular"], "images.unsplash.com")
        download = safe_url(photo["links"]["download_location"], "api.unsplash.com")
        if urlsplit(download).path != f"/photos/{identifier}/download":
            raise ValueError
        photographer = str(photo["user"]["name"])[:200]
        profile = referral(photo["user"]["links"]["html"])
        page = referral(photo["links"]["html"])
        credit = (
            f'Photo by <a href="{html.escape(profile, quote=True)}">'
            f"{html.escape(photographer)}</a> on "
            '<a href="https://unsplash.com/?utm_source=jhin&amp;utm_medium=referral">'
            "Unsplash</a>"
        )
        return {
            "photo_id": identifier,
            "image_url": image,
            "photo_url": page,
            "download_location": download,
            "photographer": photographer,
            "photographer_url": profile,
            "attribution_html": credit,
            "suggested_alt": str(photo.get("alt_description") or "")[:300],
            "width": int(photo["width"]),
            "height": int(photo["height"]),
        }
    except (KeyError, TypeError, ValueError):
        raise UnsplashError("Unsplash returned incomplete photo metadata") from None


async def request(
    access_key: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not _PATH.fullmatch(path):
        raise UnsplashError("Unsplash operation is not allowed")
    if not access_key or any(c.isspace() for c in access_key):
        raise UnsplashError("A valid Unsplash Access Key is required")
    get_redactor().register(access_key)
    if client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as owned:
            return await request(access_key, path, params, client=owned)
    req = client.build_request(
        "GET",
        API_ORIGIN + path,
        params=params,
        headers={"Authorization": "Client-ID " + access_key, "Accept-Version": "v1"},
    )
    try:
        # Read headers before bounded decoding so rate-limit metadata remains available.
        response = await client.send(req, stream=True, follow_redirects=False)
        try:
            if 300 <= response.status_code < 400:
                raise UnsplashError("Unsplash redirects are refused")
            if response.status_code == 429:
                raise UnsplashError(
                    "Unsplash rate limit reached; wait before retrying",
                    code="unsplash_rate_limited",
                    retry_after=response.headers.get("Retry-After", "")[:80],
                )
            if response.status_code >= 400:
                raise UnsplashError(
                    f"Unsplash request failed (HTTP {response.status_code})",
                    code=f"unsplash_http_{response.status_code}",
                )
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 2_000_000:
                    raise UnsplashError("Unsplash response exceeded the size limit")
            result = json.loads(chunks)
        finally:
            await response.aclose()
    except (httpx.HTTPError, ValueError):
        raise UnsplashError("Unsplash response could not be confirmed") from None
    if not isinstance(result, dict):
        raise UnsplashError("Unsplash returned an invalid response")
    return result


async def verify_access_key(
    access_key: str, *, client: httpx.AsyncClient | None = None
) -> tuple[bool, str, dict[str, str]]:
    """Probe the fixed API once, read-only, and say whether the key works.

    A one-result search is the smallest call that proves the credential: it
    reads a public index and touches no download-tracking endpoint, so a
    health check never reports a photo use to Unsplash that nobody asked for.
    Failures come back as ``ok=False`` with the connector's own wording — the
    provider's body never reaches the message.
    """
    try:
        response = await request(
            access_key,
            "/search/photos",
            {"query": "office desk", "page": 1, "per_page": 1, "content_filter": "high"},
            client=client,
        )
    except UnsplashError as error:
        return False, str(error), {}
    if not isinstance(response.get("results"), list):
        return False, "Unsplash did not return a search result page", {}
    return True, "Unsplash accepted the Access Key", {"api_origin": API_ORIGIN}
