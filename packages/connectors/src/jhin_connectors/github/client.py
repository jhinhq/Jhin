"""Thin async HTTP layer for the GitHub REST API (plan 11.2).

All requests pin the API version header, validate the exact destination
origin, and use the shared redirect-free streaming response cap. Request
headers, URLs and tokens never enter exception text.

A failed response's *message* does, on the ``detail`` channel and nowhere
else: bounded, control-stripped, and redacted where it is stored. Discarding
it made every GitHub failure a three-digit number, which cost a real run a
pull request nobody could explain — GitHub had said why, and Jhin had thrown
it away.
"""

from __future__ import annotations

from typing import Any

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_http_origin
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_tools.errors import ToolExecutionError

# Pinned GitHub REST API version (docs.github.com, current as of 2026).
API_VERSION = "2026-03-10"
DEFAULT_BASE_URL = "https://api.github.com"
USER_AGENT = "jhin-connector-github"

_TIMEOUT_SECONDS = 30.0


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _api_error_code(prefix: str, status_code: int | None) -> str:
    return f"{prefix}_http_{status_code}" if status_code is not None else f"{prefix}_request_failed"


def _side_effect_possible(method: str, status_code: int | None) -> bool:
    """Reads never have side effects; a mutation the provider definitively
    rejected (4xx) did not happen either. Only transport failures and 5xx on
    a mutation leave the outcome genuinely unknown."""
    if method.upper() in _SAFE_METHODS:
        return False
    return status_code is None or status_code >= 500


class GitHubApiError(ToolExecutionError):
    """One failed GitHub API call, with a display-safe message.

    A ``ToolExecutionError`` so the gateway records an ordinary ``failed``
    outcome (e.g. ``github_http_404``) the model can act on, instead of an
    execution-unknown reconciliation that aborts the run.

    ``provider_message`` is GitHub's own sentence about the failure and travels
    as ``detail``. A pull request refused with ``github_http_403`` and nothing
    else told the agent to retry and told the operator nothing at all; GitHub
    had already said "Resource not accessible by integration", which names the
    one thing that fixes it. It is redacted where it is stored, and it is
    evidence rather than instruction: nothing here acts on it.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str = "GET",
        code: str | None = None,
        provider_message: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code or _api_error_code("github", status_code),
            side_effect_possible=_side_effect_possible(method, status_code),
            detail=f"GitHub said: {provider_message}" if provider_message else "",
        )
        self.status_code = status_code


def validate_github_base_url(base_url: str) -> str:
    """Return a normalized approved GitHub origin without rendering the input."""
    try:
        return validate_http_origin(base_url, official_origins=(DEFAULT_BASE_URL,))
    except EndpointPolicyError:
        raise GitHubApiError(
            "GitHub API target is not allowed", code="github_target_not_allowed"
        ) from None


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


async def github_request(
    method: str,
    base_url: str,
    path: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """One authenticated JSON request. Returns the parsed body ({} for 204)."""
    safe_base_url = validate_github_base_url(base_url)
    url = f"{safe_base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            request = client.build_request(
                method,
                url,
                headers=github_headers(token),
                json=json_body,
                params=params,
            )
            payload = await send_bounded_json(client, request)
    except ProviderHTTPError as exc:
        message = "GitHub API request failed"
        if exc.status_code is not None:
            message = f"{message} with status {exc.status_code}"
        raise GitHubApiError(
            message,
            status_code=exc.status_code,
            method=method,
            provider_message=exc.provider_message,
        ) from None
    except Exception:
        raise GitHubApiError("GitHub API request failed", method=method) from None
    if payload is None:
        return {}
    return payload
