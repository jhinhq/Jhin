"""Thin async HTTP layer for the GitHub REST API (plan 11.2).

All requests pin the API version header and use httpx with explicit
timeouts. Error mapping is deliberately conservative: only the HTTP status
and GitHub's short ``message`` field (truncated) enter the exception text —
never request headers, never tokens. The process redactor scrubs anything
that slips through before persistence (plan 13.5, 48.9).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Pinned GitHub REST API version (docs.github.com, current as of 2026).
API_VERSION = "2026-03-10"
DEFAULT_BASE_URL = "https://api.github.com"
USER_AGENT = "jhin-connector-github"

_TIMEOUT_SECONDS = 30.0
_MAX_ERROR_DETAIL_CHARS = 300


class GitHubApiError(Exception):
    """One failed GitHub API call, with a display-safe message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _safe_detail(response: httpx.Response) -> str:
    """GitHub's ``message`` field when the body is JSON, else a truncated
    body preview. Bounded so hostile bodies cannot flood errors."""
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return str(payload["message"])[:_MAX_ERROR_DETAIL_CHARS]
    except (json.JSONDecodeError, ValueError):
        pass
    return response.text[:_MAX_ERROR_DETAIL_CHARS]


def error_from_response(method: str, path: str, response: httpx.Response) -> GitHubApiError:
    return GitHubApiError(
        f"GitHub API {method} {path} failed ({response.status_code}): {_safe_detail(response)}",
        status_code=response.status_code,
    )


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
    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                url,
                headers=github_headers(token),
                json=json_body,
                params=params,
            )
    except httpx.HTTPError as exc:
        raise GitHubApiError(f"GitHub API {method} {path} failed: {type(exc).__name__}") from None
    if response.status_code >= 400:
        raise error_from_response(method, path, response)
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()
