"""Shared, redirect-free, bounded provider JSON response handling.

Provider credentials belong in request headers (or, for PostgreSQL, in the
separate database client).  This module deliberately never renders a request,
URL, header, response body, or transport exception into its public errors.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from jhin_tools.sanitize import strict_json_loads

MAX_PROVIDER_RESPONSE_BYTES = 524_288


#: How much of a failed response's body is read to find out what the provider
#: said. Enough for a JSON error object, small enough that reading it is not a
#: second transfer.
MAX_PROVIDER_ERROR_BYTES = 4_096
#: How much of that survives as a sentence.
MAX_PROVIDER_MESSAGE_CHARS = 400
#: Where providers put the sentence. GitHub and Linear use ``message``, OAuth
#: servers ``error_description`` and ``error``, Supabase ``msg``/``hint``.
_MESSAGE_KEYS = ("message", "error_description", "error", "msg", "detail", "hint")


class ProviderHTTPError(Exception):
    """A display-safe provider transport or response failure.

    ``provider_message`` is the provider's own words about the failure, when
    the response carried any: bounded, stripped of anything a terminal would
    act on, and *not* part of the exception's own message. It exists because
    "GitHub API request failed with status 403" is not something anyone can
    act on, while "Resource not accessible by integration" names the fix.
    Treat it as data — it is quoted as evidence, never followed.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_message: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_message = provider_message


def _readable_message(value: Any) -> str:
    """One sentence out of a provider's error document, or nothing.

    Only the well-known keys, only at the top level and one list deep, and
    only strings. Deliberately not a search of the whole document: a body that
    echoes a request back would otherwise hand its own contents to whoever
    reads the failure.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        for key in _MESSAGE_KEYS:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                parts.append(found.strip())
        nested = value.get("errors")
        if isinstance(nested, list):
            for item in nested[:5]:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    for key in _MESSAGE_KEYS:
                        found = item.get(key)
                        if isinstance(found, str) and found.strip():
                            parts.append(found.strip())
        return "; ".join(dict.fromkeys(parts))
    return ""


def bounded_provider_message(body: bytes) -> str:
    """What a failed response says for itself, made safe to carry.

    JSON first, because that is what providers send and it is the shape that
    separates the sentence from the payload — and when the body *is* JSON,
    that separation is the only thing carried. A JSON document with none of
    the recognised keys yields no message at all rather than the document:
    the fallback used to run on an empty result rather than on an unparseable
    body, so a provider that echoed the request back handed over the whole
    echo, ``secret`` field included, and :func:`_readable_message`'s promise
    that it does not search the whole document was true of that function and
    untrue of this one. Four hundred characters of somebody's request is
    still somebody's request.

    The fallback is for a body that is not JSON, which is where a proxy or a
    gateway puts an HTML error page — worth one line, never worth the page.
    """
    if not body:
        return ""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode with 'replace' does not raise
        return ""
    try:
        message = _readable_message(strict_json_loads(text))
    except Exception:
        message = text
    flattened = "".join(character if character.isprintable() else " " for character in message)
    return " ".join(flattened.split())[:MAX_PROVIDER_MESSAGE_CHARS]


async def _error_body(response: httpx.Response) -> bytes:
    """A bounded read of a response that has already been judged a failure."""
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) >= MAX_PROVIDER_ERROR_BYTES:
                break
    except Exception:
        return bytes(body[:MAX_PROVIDER_ERROR_BYTES])
    return bytes(body[:MAX_PROVIDER_ERROR_BYTES])


def _declared_content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _parse_bounded_response(
    response: httpx.Response,
    *,
    max_response_bytes: int,
    expected_status_codes: tuple[int, ...] | None,
) -> Any:
    status_code = response.status_code
    if 300 <= status_code < 400:
        raise ProviderHTTPError(
            "Provider redirect responses are not allowed",
            status_code=status_code,
        )
    if not 200 <= status_code < 300:
        # The one place a failed body is read, bounded, and only for the
        # sentence in it. Without this a 403 crossed the boundary as three
        # digits, and the reason the operator had to go and read on GitHub —
        # "Resource not accessible by integration" — was thrown away here.
        raise ProviderHTTPError(
            f"Provider request failed with status {status_code}",
            status_code=status_code,
            provider_message=bounded_provider_message(await _error_body(response)),
        )
    if expected_status_codes is not None and status_code not in expected_status_codes:
        raise ProviderHTTPError(
            f"Provider request failed with status {status_code}",
            status_code=status_code,
        )

    declared_length = _declared_content_length(response)
    if declared_length is not None and declared_length > max_response_bytes:
        raise ProviderHTTPError(
            "Provider response is too large",
            status_code=status_code,
        )

    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_response_bytes:
                raise ProviderHTTPError(
                    "Provider response is too large",
                    status_code=status_code,
                )
            body.extend(chunk)
    except ProviderHTTPError:
        raise
    except Exception:
        raise ProviderHTTPError(
            "Provider response could not be read",
            status_code=status_code,
        ) from None

    if not body:
        return None
    try:
        return strict_json_loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProviderHTTPError(
            "Provider returned an invalid JSON response",
            status_code=status_code,
        ) from None


async def send_bounded_json(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    expected_status_codes: tuple[int, ...] | None = None,
) -> Any:
    """Send one request and return a bounded parsed JSON response.

    Redirects are never followed.  Declared and streamed response sizes are
    both enforced, and the response is closed on every path.  All failures
    crossing this boundary are stable and credential-free.
    """
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
        raise ValueError("max_response_bytes must be an integer")
    if max_response_bytes < 0:
        raise ValueError("max_response_bytes must not be negative")
    if expected_status_codes is not None and (
        not expected_status_codes
        or any(
            isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300
            for status in expected_status_codes
        )
    ):
        raise ValueError("expected_status_codes must contain 2xx integer status codes")
    if request.url.username or request.url.password:
        raise ProviderHTTPError("Provider request URL must not contain credentials")

    try:
        response = await client.send(request, stream=True, follow_redirects=False)
    except Exception:
        raise ProviderHTTPError("Provider request failed") from None

    try:
        return await _parse_bounded_response(
            response,
            max_response_bytes=max_response_bytes,
            expected_status_codes=expected_status_codes,
        )
    except ProviderHTTPError:
        raise
    except Exception:
        raise ProviderHTTPError(
            "Provider response handling failed",
            status_code=response.status_code,
        ) from None
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            await response.aclose()
        except Exception:
            if not active_exception:
                raise ProviderHTTPError(
                    "Provider response could not be closed",
                    status_code=response.status_code,
                ) from None
