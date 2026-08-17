"""Thin async client for the Linear GraphQL API (plan 11.3).

Linear's API is a single GraphQL endpoint (``POST /graphql``). Personal API
keys are sent as the bare ``Authorization`` header value — no ``Bearer``
prefix (that is Linear's documented scheme for API keys; OAuth access tokens
would use ``Bearer`` when OAuth support arrives).

Error mapping is conservative: only the HTTP status and the first GraphQL
error message (truncated) enter exception text — never the request payload,
never the API key. The process redactor scrubs anything that slips through
before persistence (plan 13.5, 48.9).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.linear.app"
USER_AGENT = "jhin-connector-linear"

AUTH_API_KEY = "api_key"
AUTH_OAUTH = "oauth"

_TIMEOUT_SECONDS = 30.0
_MAX_ERROR_DETAIL_CHARS = 300


class LinearApiError(Exception):
    """One failed Linear API call, with a display-safe message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def linear_headers(api_key: str) -> dict[str, str]:
    # Personal API keys go into Authorization verbatim (no Bearer prefix).
    return {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _first_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message", "unknown GraphQL error"))
    return "unknown GraphQL error"


async def linear_graphql(
    base_url: str,
    api_key: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One authenticated GraphQL request. Returns the ``data`` object.

    Raises :class:`LinearApiError` for transport failures, non-2xx statuses,
    and GraphQL-level errors.
    """
    url = f"{base_url.rstrip('/')}/graphql"
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=linear_headers(api_key), json=body)
    except httpx.HTTPError as exc:
        raise LinearApiError(f"Linear API request failed: {type(exc).__name__}") from None
    if response.status_code >= 400:
        raise LinearApiError(
            f"Linear API request failed ({response.status_code}): "
            f"{response.text[:_MAX_ERROR_DETAIL_CHARS]}",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        raise LinearApiError("Linear API returned a non-JSON response") from None
    if not isinstance(payload, dict):
        raise LinearApiError("Linear API returned an unexpected response shape")
    if payload.get("errors"):
        raise LinearApiError(
            f"Linear GraphQL error: {_first_error_message(payload)[:_MAX_ERROR_DETAIL_CHARS]}",
            status_code=response.status_code,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LinearApiError("Linear API response is missing the data object")
    return data
