"""Bounded, redirect-free outbound HTTP for the OAuth core.

This is the same posture as :func:`jhin_connectors.http_client.send_bounded_json`
— explicit timeout, redirects never followed, declared *and* streamed size
enforced, response closed on every path, credential-free errors — with one
difference that OAuth cannot do without: an error *body* is parsed and
returned rather than collapsed into a status code.

The whole refresh taxonomy turns on telling ``invalid_grant`` (re-authorize,
stop retrying) from ``invalid_client`` (re-register once) from
``authorization_pending`` (poll again), and every one of those arrives as a
JSON body on an HTTP 400 — or, for GitHub's device flow, on an HTTP 200.
Collapsing them would leave the classification guessing. Nothing else about
the shared helper's contract is relaxed here.

Private to :mod:`jhin_oauth`.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from jhin_tools.sanitize import strict_json_loads

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
MAX_METADATA_BYTES: Final[int] = 65_536
MAX_TOKEN_BYTES: Final[int] = 32_768

JSON_ACCEPT: Final[str] = "application/json"


class BoundedHttpError(Exception):
    """A transport-level failure: no usable HTTP response was obtained.

    ``transient`` distinguishes "try again later" (connect/read failure) from
    "this endpoint is unusable" (a redirect, an oversized body). The message
    is a constant; the URL, headers, and body never appear in it.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class BoundedResponse:
    """One received response, bounded and parsed as far as it can be."""

    status_code: int
    payload: Any
    """Parsed JSON, or ``None`` when the body was empty or not JSON. A body
    that fails to parse is not an exception here: an error response with an
    unparseable body is a classification input, not a transport failure."""
    content_type: str
    headers: Mapping[str, str]
    """Response header names, lowercased."""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_json(self) -> bool:
        """Whether the declared media type is JSON or a ``+json`` structured
        syntax suffix, per RFC 6839."""
        media_type = self.content_type.split(";", 1)[0].strip().lower()
        return media_type == JSON_ACCEPT or media_type.endswith("+json")


def _declared_content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _read_bounded_body(response: httpx.Response, *, max_response_bytes: int) -> bytes:
    declared = _declared_content_length(response)
    if declared is not None and declared > max_response_bytes:
        raise BoundedHttpError("the response is too large", transient=False)
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_response_bytes:
                raise BoundedHttpError("the response is too large", transient=False)
            body.extend(chunk)
    except BoundedHttpError:
        raise
    except Exception:
        raise BoundedHttpError("the response could not be read", transient=True) from None
    return bytes(body)


def _parse_payload(body: bytes) -> Any:
    if not body:
        return None
    try:
        return strict_json_loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


async def send_bounded(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    max_response_bytes: int,
) -> BoundedResponse:
    """Send one request and return a bounded response, body included.

    Redirects are never followed and a 3xx is refused outright: an OAuth
    endpoint that redirects is either misconfigured or being used to walk Jhin
    somewhere the SSRF policy already refused, and following it would launder
    that refusal into a fetch.
    """
    if request.url.username or request.url.password:
        raise BoundedHttpError("the request URL must not contain credentials", transient=False)
    try:
        response = await client.send(request, stream=True, follow_redirects=False)
    except Exception:
        raise BoundedHttpError("the request failed", transient=True) from None

    try:
        if 300 <= response.status_code < 400:
            raise BoundedHttpError("redirect responses are not allowed", transient=False)
        body = await _read_bounded_body(response, max_response_bytes=max_response_bytes)
        return BoundedResponse(
            status_code=response.status_code,
            payload=_parse_payload(body),
            content_type=response.headers.get("content-type", ""),
            headers={name.lower(): value for name, value in response.headers.items()},
        )
    except BoundedHttpError:
        raise
    except Exception:
        raise BoundedHttpError("the response could not be handled", transient=True) from None
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            await response.aclose()
        except Exception:
            if not active_exception:
                raise BoundedHttpError("the response could not be closed", transient=True) from None


def build_json_get(client: httpx.AsyncClient, url: str) -> httpx.Request:
    """A metadata GET with an explicit timeout and a JSON Accept header."""
    return client.build_request(
        "GET",
        url,
        headers={"Accept": JSON_ACCEPT},
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


def build_form_post(
    client: httpx.AsyncClient,
    url: str,
    form: Mapping[str, str],
    *,
    headers: Mapping[str, str] | None = None,
) -> httpx.Request:
    """A token-style POST: form-encoded body, never a query string.

    Credential material travels in the body precisely so it cannot end up in
    an access log, a ``Referer``, or a proxy's URL history.
    """
    return client.build_request(
        "POST",
        url,
        data=dict(form),
        headers={"Accept": JSON_ACCEPT, **dict(headers or {})},
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


def build_json_post(
    client: httpx.AsyncClient,
    url: str,
    document: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
) -> httpx.Request:
    """A registration POST carrying a JSON document."""
    return client.build_request(
        "POST",
        url,
        json=dict(document),
        headers={"Accept": JSON_ACCEPT, **dict(headers or {})},
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "JSON_ACCEPT",
    "MAX_METADATA_BYTES",
    "MAX_TOKEN_BYTES",
    "BoundedHttpError",
    "BoundedResponse",
    "build_form_post",
    "build_json_get",
    "build_json_post",
    "send_bounded",
]
