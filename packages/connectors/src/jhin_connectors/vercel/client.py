"""Bounded, redirect-free Vercel REST API client."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_http_origin
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json

DEFAULT_BASE_URL = "https://api.vercel.com"
USER_AGENT = "jhin-connector-vercel"
MAX_LIST_ROWS = 200
MAX_LIST_PAGES = 5
MAX_PAGE_ROWS = 100

_CONNECT_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 20.0
_TEAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class VercelApiError(Exception):
    """A stable, credential-free Vercel failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_vercel_base_url(base_url: str) -> str:
    """Return an exact approved Vercel API origin without echoing input."""
    try:
        return validate_http_origin(base_url, official_origins=(DEFAULT_BASE_URL,))
    except EndpointPolicyError:
        raise VercelApiError(
            "Vercel API target is not allowed",
            code="endpoint_not_allowed",
        ) from None


def validate_team_id(value: Any) -> str:
    """Validate a configured public team identifier before it enters a query."""
    if not isinstance(value, str) or not _TEAM_ID_RE.fullmatch(value):
        raise ValueError("config field 'team_id' is invalid")
    return value


def _bounded_cursor(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise VercelApiError(
            "Vercel returned invalid pagination metadata",
            code="invalid_pagination",
        )
    cursor = str(value)
    if not cursor or len(cursor) > 200:
        raise VercelApiError(
            "Vercel returned invalid pagination metadata",
            code="invalid_pagination",
        )
    return cursor


def _next_cursor(payload: dict[str, Any]) -> str | None:
    pagination = payload.get("pagination")
    if pagination is None:
        return None
    if not isinstance(pagination, dict):
        raise VercelApiError(
            "Vercel returned invalid pagination metadata",
            code="invalid_pagination",
        )
    return _bounded_cursor(
        pagination.get("next") if pagination.get("next") is not None else pagination.get("until")
    )


def _object_list(payload: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise VercelApiError(
            "Vercel returned an unexpected response shape",
            code="invalid_provider_response",
        )
    rows = payload.get(field)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise VercelApiError(
            "Vercel returned an unexpected response shape",
            code="invalid_provider_response",
        )
    return rows


def deployment_project_id(deployment: dict[str, Any]) -> str:
    top_level = deployment.get("projectId")
    nested_project = deployment.get("project")
    nested = nested_project.get("id") if isinstance(nested_project, dict) else None
    values: list[str] = []
    for candidate in (top_level, nested):
        if candidate is None:
            continue
        if not isinstance(candidate, str) or not candidate or len(candidate) > 200:
            raise VercelApiError(
                "Vercel returned an unexpected deployment shape",
                code="invalid_provider_response",
            )
        values.append(candidate)
    if not values:
        raise VercelApiError(
            "Vercel returned an unexpected deployment shape",
            code="invalid_provider_response",
        )
    if any(value != values[0] for value in values[1:]):
        raise VercelApiError(
            "Vercel deployment project ownership is inconsistent",
            code="project_scope_mismatch",
        )
    return values[0]


class VercelClient:
    """One connection-scoped Vercel client.

    The endpoint is revalidated for every request, covering legacy rows that
    predate create-time validation. Every response is streamed through the
    shared 512 KiB JSON cap and closed on all paths.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        team_id: str = "",
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._team_id = team_id

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] | None = None,
    ) -> Any:
        safe_base_url = validate_vercel_base_url(self._base_url)
        query = dict(params or {})
        if self._team_id:
            query["teamId"] = self._team_id
        timeout = httpx.Timeout(
            _TOTAL_TIMEOUT_SECONDS,
            connect=_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                request = client.build_request(
                    method,
                    f"{safe_base_url}{path}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    params=query or None,
                    json=json_body,
                )
                return await send_bounded_json(
                    client,
                    request,
                    expected_status_codes=expected_status_codes,
                )
        except VercelApiError:
            raise
        except ProviderHTTPError as exc:
            if exc.status_code is not None and 300 <= exc.status_code < 400:
                code = "provider_redirect"
                message = "Vercel redirect responses are not allowed"
            elif exc.status_code is None or 200 <= exc.status_code < 300:
                code = "provider_transport_error"
                message = "Vercel API request failed"
            else:
                code = "provider_http_error"
                message = f"Vercel API request failed with status {exc.status_code}"
            raise VercelApiError(
                message,
                code=code,
                status_code=exc.status_code,
            ) from None
        except Exception:
            raise VercelApiError(
                "Vercel API request failed",
                code="provider_transport_error",
            ) from None

    async def get_user(self) -> dict[str, Any]:
        payload = await self.request("GET", "/v2/user")
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected user response",
                code="invalid_provider_response",
            )
        return payload

    async def get_project(self, project_id: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/v9/projects/{quote(project_id, safe='')}")
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected project response",
                code="invalid_provider_response",
            )
        return payload

    async def list_projects(self, *, limit: int) -> tuple[list[dict[str, Any]], bool]:
        bounded_limit = min(limit, MAX_LIST_ROWS)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        truncated = False
        for _page_number in range(MAX_LIST_PAGES):
            remaining = bounded_limit - len(rows)
            if remaining <= 0:
                truncated = cursor is not None
                break
            params: dict[str, Any] = {"limit": min(MAX_PAGE_ROWS, remaining)}
            if cursor is not None:
                params["until"] = cursor
            payload = await self.request("GET", "/v9/projects", params=params)
            page_rows = _object_list(payload, "projects")
            next_cursor = _next_cursor(payload)
            rows.extend(page_rows[:remaining])
            if len(page_rows) > remaining:
                truncated = True
                break
            if next_cursor is None:
                break
            if next_cursor in seen or next_cursor == cursor:
                raise VercelApiError(
                    "Vercel returned a repeated pagination cursor",
                    code="invalid_pagination",
                )
            seen.add(next_cursor)
            cursor = next_cursor
            if len(rows) >= bounded_limit:
                truncated = True
                break
        else:
            truncated = cursor is not None
        return rows[:bounded_limit], truncated

    async def list_deployments(
        self,
        *,
        project_id: str,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        bounded_limit = min(limit, MAX_LIST_ROWS)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        truncated = False
        for _page_number in range(MAX_LIST_PAGES):
            remaining = bounded_limit - len(rows)
            if remaining <= 0:
                truncated = cursor is not None
                break
            params: dict[str, Any] = {
                "projectId": project_id,
                "limit": min(MAX_PAGE_ROWS, remaining),
            }
            if cursor is not None:
                params["until"] = cursor
            payload = await self.request("GET", "/v6/deployments", params=params)
            page_rows = _object_list(payload, "deployments")
            # Validate the entire page before exposing even its first row.
            for row in page_rows:
                if deployment_project_id(row) != project_id:
                    raise VercelApiError(
                        "Vercel deployment does not belong to the requested project",
                        code="project_scope_mismatch",
                    )
            next_cursor = _next_cursor(payload)
            rows.extend(page_rows[:remaining])
            if len(page_rows) > remaining:
                truncated = True
                break
            if next_cursor is None:
                break
            if next_cursor in seen or next_cursor == cursor:
                raise VercelApiError(
                    "Vercel returned a repeated pagination cursor",
                    code="invalid_pagination",
                )
            seen.add(next_cursor)
            cursor = next_cursor
            if len(rows) >= bounded_limit:
                truncated = True
                break
        else:
            truncated = cursor is not None
        return rows[:bounded_limit], truncated

    async def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        payload = await self.request("GET", f"/v13/deployments/{quote(deployment_id, safe='')}")
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected deployment response",
                code="invalid_provider_response",
            )
        return payload

    async def get_deployment_events(
        self,
        deployment_id: str,
        *,
        since: int,
        until: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"/v3/deployments/{quote(deployment_id, safe='')}/events",
            params={
                "since": since,
                "until": until,
                "limit": min(limit, 200),
                "follow": 0,
            },
        )
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise VercelApiError(
                "Vercel returned an unexpected build-log response",
                code="invalid_provider_response",
            )
        return payload

    async def get_environment(self, project_id: str) -> list[dict[str, Any]]:
        payload = await self.request("GET", f"/v9/projects/{quote(project_id, safe='')}/env")
        return _object_list(payload, "envs")

    async def create_deployment(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            "/v13/deployments",
            json_body=body,
            expected_status_codes=(200, 201),
        )
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected deployment response",
                code="invalid_provider_response",
            )
        return payload

    async def redeploy(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            "/v13/deployments",
            params={"forceNew": 1},
            json_body=body,
            expected_status_codes=(200, 201),
        )
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected deployment response",
                code="invalid_provider_response",
            )
        return payload

    async def promote(self, project_id: str, deployment_id: str) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            (f"/v10/projects/{quote(project_id, safe='')}/promote/{quote(deployment_id, safe='')}"),
            json_body={},
            expected_status_codes=(200, 201),
        )
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected deployment response",
                code="invalid_provider_response",
            )
        return payload

    async def assign_alias(self, deployment_id: str, alias: str) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            f"/v2/deployments/{quote(deployment_id, safe='')}/aliases",
            json_body={"alias": alias},
            expected_status_codes=(200, 201),
        )
        if not isinstance(payload, dict):
            raise VercelApiError(
                "Vercel returned an unexpected alias response",
                code="invalid_provider_response",
            )
        return payload
