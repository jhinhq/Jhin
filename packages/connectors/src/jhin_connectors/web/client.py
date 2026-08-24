"""Backend adapters, URL/domain policy, and bounded transport for the web
connector (docs/architecture/web.md).

Security posture mirrors the generic HTTP connector:

- search requests only ever go to the backend's official https origin or an
  operator-allow-listed override (``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS``);
- ``web.fetch`` accepts public http(s) URLs through the shared
  :func:`validate_public_http_url` policy, optionally narrowed by the
  connection's ``allowed_domains`` glob patterns;
- requests are GET/POST with fixed shapes, never follow cross-origin
  redirects, and read bounded bytes under a timeout;
- no error crossing this module carries a credential.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_public_http_url
from jhin_connectors.web.manifest import BACKEND_BRAVE, BACKEND_EXA, BACKEND_TAVILY, SEARCH_BACKENDS
from jhin_connectors.web.schemas import (
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    WebSearchResult,
)

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_FETCH_BYTES = 262_144
MAX_SAME_ORIGIN_REDIRECTS = 3
MAX_ALLOWED_DOMAIN_PATTERNS = 50
_DOMAIN_PATTERN_RE = re.compile(r"^[a-z0-9.*-]{1,253}$")

# Official search API origins (path shapes follow each provider's real API).
DEFAULT_BASE_URLS: dict[str, str] = {
    BACKEND_TAVILY: "https://api.tavily.com",
    BACKEND_BRAVE: "https://api.search.brave.com",
    BACKEND_EXA: "https://api.exa.ai",
}


def validate_backend(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in SEARCH_BACKENDS:
        allowed = ", ".join(SEARCH_BACKENDS)
        raise ValueError(f"config field 'search_backend' must be one of: {allowed}")
    return value


def backend_base_url(backend: str, config: Mapping[str, Any]) -> str:
    """The search API origin: the official endpoint, or a policy-checked
    override (dev doubles reach the fake through the operator allow-list)."""
    override = str(config.get("base_url") or "").strip()
    if not override:
        return DEFAULT_BASE_URLS[backend]
    return validate_public_http_url(
        override, kind="Web API base URL", allowlist_env=ALLOWLIST_ENV
    ).rstrip("/")


def validate_allowed_domains(raw: object) -> list[str]:
    """Normalized host glob patterns from ``config_json.allowed_domains``."""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("config field 'allowed_domains' must be a list of host patterns")
    if len(raw) > MAX_ALLOWED_DOMAIN_PATTERNS:
        raise ValueError(
            f"config field 'allowed_domains' accepts at most {MAX_ALLOWED_DOMAIN_PATTERNS} entries"
        )
    patterns: list[str] = []
    for entry in raw:
        candidate = str(entry).strip().lower()
        if not candidate or not _DOMAIN_PATTERN_RE.fullmatch(candidate):
            raise ValueError(
                "allowed_domains entries must be host patterns like docs.python.org "
                "or *.wikipedia.org"
            )
        patterns.append(candidate)
    return patterns


def domain_allowed(host: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    candidate = host.lower()
    return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns)


def search_token(credentials: Mapping[str, str]) -> str:
    token = credentials.get("token", "")
    if not token:
        raise ValueError("this web connection stores no search API key")
    if any(character in token for character in "\r\n\0"):
        raise ValueError("this web connection's search API key is malformed")
    return token


@dataclass(frozen=True)
class SearchRequestSpec:
    """One backend-shaped search request, ready for the bounded client."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None


def build_search_request(
    backend: str, base_url: str, token: str, query: str, max_results: int
) -> SearchRequestSpec:
    """The provider's real wire shape for one bounded search call."""
    if backend == BACKEND_TAVILY:
        # POST https://api.tavily.com/search with a bearer token.
        return SearchRequestSpec(
            method="POST",
            url=f"{base_url}/search",
            headers={"Authorization": f"Bearer {token}"},
            json_body={"query": query, "max_results": max_results},
        )
    if backend == BACKEND_BRAVE:
        # GET https://api.search.brave.com/res/v1/web/search with X-Subscription-Token.
        return SearchRequestSpec(
            method="GET",
            url=f"{base_url}/res/v1/web/search",
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
            params={"q": query, "count": str(max_results)},
        )
    if backend == BACKEND_EXA:
        # POST https://api.exa.ai/search with an x-api-key header.
        return SearchRequestSpec(
            method="POST",
            url=f"{base_url}/search",
            headers={"x-api-key": token},
            json_body={"query": query, "numResults": max_results},
        )
    raise ValueError(f"unsupported search backend {backend!r}")


def _clean_result(
    item: Mapping[str, Any], *, snippet_keys: tuple[str, ...]
) -> WebSearchResult | None:
    url = item.get("url")
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None
    title = item.get("title")
    snippet = next(
        (item[key] for key in snippet_keys if isinstance(item.get(key), str) and item[key]), ""
    )
    published_raw = next(
        (
            item[key]
            for key in ("published_date", "publishedDate", "page_age", "age")
            if isinstance(item.get(key), str) and item[key]
        ),
        None,
    )
    return WebSearchResult(
        title=(title if isinstance(title, str) else "")[:MAX_TITLE_CHARS],
        url=url[:MAX_URL_CHARS],
        snippet=str(snippet)[:MAX_SNIPPET_CHARS],
        published=published_raw[:100] if isinstance(published_raw, str) else None,
    )


def parse_search_results(backend: str, payload: Any, limit: int) -> list[WebSearchResult]:
    """Normalize one backend response to the bounded common shape."""
    rows: Any = []
    if isinstance(payload, dict):
        if backend == BACKEND_BRAVE:
            web = payload.get("web")
            rows = web.get("results") if isinstance(web, dict) else []
        else:
            rows = payload.get("results")
    snippet_keys = {
        BACKEND_TAVILY: ("content", "snippet"),
        BACKEND_BRAVE: ("description", "snippet"),
        BACKEND_EXA: ("text", "snippet", "summary"),
    }[backend]
    results: list[WebSearchResult] = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, Mapping):
            continue
        cleaned = _clean_result(item, snippet_keys=snippet_keys)
        if cleaned is not None:
            results.append(cleaned)
        if len(results) >= limit:
            break
    return results


def http_client(headers: Mapping[str, str] | None = None) -> httpx.AsyncClient:
    """A bounded client that never follows redirects on its own."""
    return httpx.AsyncClient(
        follow_redirects=False,
        headers=dict(headers or {}),
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


# --- web.fetch -----------------------------------------------------------------


def validate_fetch_url(raw: str, allowed_domains: list[str]) -> str:
    """Policy-checked fetch URL: public http(s) via the shared policy, then
    the connection's optional domain narrowing."""
    normalized = validate_public_http_url(raw, kind="Fetch URL", allowlist_env=ALLOWLIST_ENV)
    host = (urlsplit(normalized).hostname or "").lower()
    if not domain_allowed(host, allowed_domains):
        raise EndpointPolicyError("Fetch URL host is outside this connection's allowed domains")
    return normalized


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    return scheme, (parts.hostname or "").lower(), port


_TEXTUAL_SUFFIXES = ("+json", "+xml")
_TEXTUAL_TYPES = frozenset({"application/json", "application/xml", "application/xhtml+xml", ""})


def is_textual_media(media: str) -> bool:
    return media.startswith("text/") or media in _TEXTUAL_TYPES or media.endswith(_TEXTUAL_SUFFIXES)


@dataclass(frozen=True)
class FetchedPage:
    """One bounded response body (text only; binary is flagged, not read)."""

    final_url: str
    status_code: int
    content_type: str
    media: str
    body_text: str
    bytes_truncated: bool
    is_binary: bool


async def fetch_page(
    client: httpx.AsyncClient, url: str, allowed_domains: list[str]
) -> FetchedPage:
    """GET one page, following at most a few *same-origin* redirects.

    A redirect that leaves the origin is never followed — the redirect
    status is returned instead, so nothing outside the validated origin
    (and the connection's allowed domains) is ever requested.
    """
    current = url
    for _hop in range(MAX_SAME_ORIGIN_REDIRECTS + 1):
        response = await client.send(client.build_request("GET", current), stream=True)
        try:
            status = response.status_code
            location = response.headers.get("location", "")
            if status in (301, 302, 303, 307, 308) and location:
                target = urljoin(current, location)
                try:
                    validated = validate_fetch_url(target, allowed_domains)
                except EndpointPolicyError:
                    return FetchedPage(current, status, "", "", "", False, False)
                if _origin(validated) != _origin(current) or _hop == MAX_SAME_ORIGIN_REDIRECTS:
                    return FetchedPage(current, status, "", "", "", False, False)
                current = validated
                continue
            content_type = response.headers.get("content-type", "")
            media = content_type.split(";", 1)[0].strip().lower()
            if not is_textual_media(media):
                return FetchedPage(current, status, content_type, media, "", False, True)
            collected = bytearray()
            async for chunk in response.aiter_bytes():
                collected.extend(chunk)
                if len(collected) > MAX_FETCH_BYTES:
                    break
            truncated = len(collected) > MAX_FETCH_BYTES
            encoding = response.charset_encoding or "utf-8"
            try:
                text = bytes(collected[:MAX_FETCH_BYTES]).decode(encoding, errors="replace")
            except LookupError:
                text = bytes(collected[:MAX_FETCH_BYTES]).decode("utf-8", errors="replace")
            return FetchedPage(current, status, content_type, media, text, truncated, False)
        finally:
            await response.aclose()
    raise AssertionError("unreachable: redirect loop is bounded")  # pragma: no cover
