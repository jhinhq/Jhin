"""URL policy, path joining, header hygiene, auth, and bounded transport.

Security posture mirrors the other connectors' HTTP clients (plan 11.7):

- the base URL is validated before persistence *and* before every request:
  public ``https`` origins are allowed; anything else (plain http,
  localhost, private ranges, non-global IP literals) must be in the
  operator allow-list ``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS``;
- request paths are joined to the base URL — absolute URLs, ``..``
  segments, and embedded queries/fragments are rejected, so a grant's path
  pattern cannot be escaped;
- caller headers are allow-listed by shape and may never carry
  authentication or cookies (auth comes only from the connection's scheme);
- redirects are never followed; every request is bounded by a timeout and
  the response body is read up to a fixed byte budget only;
- no error crossing this module carries the URL, headers, or a credential.
"""

from __future__ import annotations

import base64
import posixpath
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from jhin_connectors.endpoints import validate_public_http_url
from jhin_connectors.http.manifest import AUTH_BASIC, AUTH_BEARER, AUTH_HEADER, AUTH_NONE

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TEXT_CHARS = 20_000
MAX_RESPONSE_BYTES = 262_144
TRUNCATION_MARKER = "…[truncated]"

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
# Headers a connection or call may never set: authentication belongs to the
# auth scheme, and the transport owns the connection-level fields.
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "expect",
        "keep-alive",
        "proxy-connection",
    }
)


def validate_http_base_url(raw: str, *, allowlist_env: str = ALLOWLIST_ENV) -> str:
    """Policy-checked base URL (shared policy with the MCP connector)."""
    return validate_public_http_url(raw, kind="HTTP base URL", allowlist_env=allowlist_env)


def validate_request_header_name(name: object) -> str:
    if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
        raise ValueError("HTTP header names must be short ASCII names like X-Request-Id")
    if name.lower() in _FORBIDDEN_HEADERS:
        raise ValueError(
            f"the {name!r} header is reserved; authentication comes from the "
            "connection's auth scheme"
        )
    return name


def _validate_header_value(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2_000
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError("HTTP header values must be single-line strings")
    return value


def request_headers(raw: Mapping[str, object]) -> dict[str, str]:
    """Validated non-secret headers from one tool call."""
    return {
        validate_request_header_name(name): _validate_header_value(value)
        for name, value in raw.items()
    }


def default_headers_from_config(config: Mapping[str, Any]) -> dict[str, str]:
    """Validated non-secret headers from ``config_json.default_headers``
    (``string_list`` entries shaped ``Name: value``)."""
    raw = config.get("default_headers") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("config field 'default_headers' must be a list of 'Name: value' lines")
    headers: dict[str, str] = {}
    for entry in raw:
        name, sep, value = str(entry).partition(":")
        if not sep:
            raise ValueError("default_headers entries must look like 'Name: value'")
        headers[validate_request_header_name(name.strip())] = _validate_header_value(value.strip())
    return headers


def auth_headers(
    auth_type: str, credentials: Mapping[str, str], config: Mapping[str, Any]
) -> dict[str, str]:
    """Request headers for one connection's auth scheme. Raises ValueError
    with a credential-free message when the stored material is unusable."""
    if auth_type == AUTH_NONE:
        return {}
    if auth_type == AUTH_BASIC:
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not username or not password:
            raise ValueError("this HTTP connection stores no basic-auth credentials")
        if any(character in username + password for character in "\r\n\0") or ":" in username:
            raise ValueError("this HTTP connection's basic-auth credentials are malformed")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    token = credentials.get("token", "")
    if not token:
        raise ValueError("this HTTP connection stores no token")
    if any(character in token for character in "\r\n\0"):
        raise ValueError("this HTTP connection's token is malformed")
    if auth_type == AUTH_BEARER:
        return {"Authorization": f"Bearer {token}"}
    if auth_type == AUTH_HEADER:
        return {validate_request_header_name(config.get("header_name")): token}
    raise ValueError(f"unsupported HTTP auth type {auth_type!r}")


def join_url(base_url: str, path: str) -> str:
    """Join a relative request path to the connection's validated base URL.

    Absolute URLs, protocol-relative paths, ``..`` segments, embedded
    queries/fragments, and control characters are rejected: the path a grant
    pattern matched must be exactly the path that is requested, and it can
    never escape the base URL.
    """
    candidate = path or "/"
    if "\\" in candidate or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in candidate
    ):
        raise ValueError("path contains whitespace or control characters")
    if "?" in candidate or "#" in candidate:
        raise ValueError("path must not embed a query or fragment; use the query field")
    if "://" in candidate or candidate.startswith("//"):
        raise ValueError("path must be relative to the connection's base URL")
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    if ".." in candidate.split("/"):
        raise ValueError("path must not contain '..' segments")
    normalized = posixpath.normpath(candidate)
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or ".." in normalized.split("/")
    ):
        raise ValueError("path is invalid")
    if candidate.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, base_path + normalized, "", ""))


def http_client(headers: Mapping[str, str] | None = None) -> httpx.AsyncClient:
    """A bounded client that never follows redirects."""
    return httpx.AsyncClient(
        follow_redirects=False,
        headers=dict(headers or {}),
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


def _is_textual(media: str) -> bool:
    return (
        media == ""
        or media.startswith("text/")
        or media == "application/json"
        or media.endswith("+json")
    )


async def send_bounded_text(
    client: httpx.AsyncClient, request: httpx.Request
) -> tuple[int, str, str, bool]:
    """``(status, content_type, text, truncated)`` with a hard byte budget.

    Only text and JSON bodies are surfaced; anything else becomes a
    placeholder so binary responses can never flood the transcript.
    """
    response = await client.send(request, stream=True)
    try:
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        media = content_type.split(";", 1)[0].strip().lower()
        if not _is_textual(media):
            placeholder = f"[response body omitted: unsupported content type {media}]"
            return status, content_type, placeholder, False
        collected = bytearray()
        async for chunk in response.aiter_bytes():
            collected.extend(chunk)
            if len(collected) > MAX_RESPONSE_BYTES:
                break
        truncated = len(collected) > MAX_RESPONSE_BYTES
        encoding = response.charset_encoding or "utf-8"
        try:
            text = bytes(collected[:MAX_RESPONSE_BYTES]).decode(encoding, errors="replace")
        except LookupError:
            text = bytes(collected[:MAX_RESPONSE_BYTES]).decode("utf-8", errors="replace")
        if len(text) > MAX_TEXT_CHARS:
            truncated = True
            text = text[:MAX_TEXT_CHARS]
        if truncated:
            text += TRUNCATION_MARKER
        return status, content_type, text, truncated
    finally:
        await response.aclose()
