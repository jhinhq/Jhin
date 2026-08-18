"""Thin async client for the Linear GraphQL API (plan 11.3).

Linear's API is a single GraphQL endpoint (``POST /graphql``). Personal API
keys are sent as the bare ``Authorization`` header value — no ``Bearer``
prefix (that is Linear's documented scheme for API keys; OAuth access tokens
would use ``Bearer`` when OAuth support arrives).

The exact destination origin is validated at the final outbound boundary,
and every response uses the shared redirect-free streaming cap. Error
mapping never includes provider bodies, request payloads, URLs, or API keys.
"""

from __future__ import annotations

from typing import Any

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_http_origin
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json

DEFAULT_BASE_URL = "https://api.linear.app"
USER_AGENT = "jhin-connector-linear"

AUTH_API_KEY = "api_key"
AUTH_OAUTH = "oauth"

_TIMEOUT_SECONDS = 30.0


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


def validate_linear_base_url(base_url: str) -> str:
    """Return a normalized approved Linear origin without rendering the input."""
    try:
        return validate_http_origin(base_url, official_origins=(DEFAULT_BASE_URL,))
    except EndpointPolicyError:
        raise LinearApiError("Linear API target is not allowed") from None


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
    safe_base_url = validate_linear_base_url(base_url)
    url = f"{safe_base_url}/graphql"
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            request = client.build_request(
                "POST",
                url,
                headers=linear_headers(api_key),
                json=body,
            )
            payload = await send_bounded_json(client, request)
    except ProviderHTTPError as exc:
        raise LinearApiError(
            f"Linear API request failed: {exc}",
            status_code=exc.status_code,
        ) from None
    except Exception:
        raise LinearApiError("Linear API request failed") from None
    if not isinstance(payload, dict):
        raise LinearApiError("Linear API returned an unexpected response shape")
    if payload.get("errors"):
        raise LinearApiError(
            "Linear GraphQL request failed",
            status_code=200,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LinearApiError("Linear API response is missing the data object")
    return data
