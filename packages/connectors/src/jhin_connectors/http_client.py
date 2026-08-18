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


class ProviderHTTPError(Exception):
    """A display-safe provider transport or response failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        raise ProviderHTTPError(
            f"Provider request failed with status {status_code}",
            status_code=status_code,
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
