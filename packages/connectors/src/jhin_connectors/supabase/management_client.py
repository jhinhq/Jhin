"""Bounded Supabase Management API client."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from jhin_connectors.endpoints import EndpointPolicyError, validate_http_origin
from jhin_connectors.http_client import ProviderHTTPError, send_bounded_json
from jhin_connectors.supabase.manifest import DEFAULT_BASE_URL

USER_AGENT = "jhin-connector-supabase"
_CONNECT_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 20.0


class SupabaseManagementError(Exception):
    """A stable, credential-free Management API failure."""

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


def validate_supabase_base_url(base_url: str) -> str:
    try:
        return validate_http_origin(base_url, official_origins=(DEFAULT_BASE_URL,))
    except EndpointPolicyError:
        raise SupabaseManagementError(
            "Supabase Management API target is not allowed",
            code="endpoint_not_allowed",
        ) from None


class SupabaseManagementClient:
    """One connection-scoped, redirect-free Management API client."""

    def __init__(self, *, base_url: str, access_token: str) -> None:
        self._base_url = base_url
        self._access_token = access_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str | None, bytes, str]]] | None = None,
        expected_status_codes: tuple[int, ...] | None = None,
    ) -> Any:
        safe_base_url = validate_supabase_base_url(self._base_url)
        timeout = httpx.Timeout(_TOTAL_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(timeout=timeout) as client:
                    request = client.build_request(
                        method,
                        f"{safe_base_url}{path}",
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {self._access_token}",
                            "User-Agent": USER_AGENT,
                        },
                        params=params,
                        files=files,
                    )
                    return await send_bounded_json(
                        client,
                        request,
                        expected_status_codes=expected_status_codes,
                    )
        except SupabaseManagementError:
            raise
        except ProviderHTTPError as exc:
            if exc.status_code is not None and 300 <= exc.status_code < 400:
                code = "provider_redirect"
                message = "Supabase Management API redirects are not allowed"
            elif exc.status_code is None or 200 <= exc.status_code < 300:
                code = "provider_transport_error"
                message = "Supabase Management API request failed"
            else:
                code = "provider_http_error"
                message = f"Supabase Management API request failed with status {exc.status_code}"
            raise SupabaseManagementError(
                message,
                code=code,
                status_code=exc.status_code,
            ) from None
        except Exception:
            raise SupabaseManagementError(
                "Supabase Management API request failed",
                code="provider_transport_error",
            ) from None

    async def get_project(self, project_ref: str) -> dict[str, Any]:
        payload = await self.request(
            "GET",
            f"/v1/projects/{quote(project_ref, safe='')}",
        )
        if not isinstance(payload, dict):
            raise SupabaseManagementError(
                "Supabase returned an unexpected project response",
                code="invalid_provider_response",
            )
        return payload

    async def get_logs(
        self,
        project_ref: str,
        *,
        sql: str,
        iso_timestamp_start: str,
        iso_timestamp_end: str,
    ) -> dict[str, Any]:
        payload = await self.request(
            "GET",
            f"/v1/projects/{quote(project_ref, safe='')}/analytics/endpoints/logs",
            params={
                "sql": sql,
                "iso_timestamp_start": iso_timestamp_start,
                "iso_timestamp_end": iso_timestamp_end,
            },
        )
        if not isinstance(payload, dict):
            raise SupabaseManagementError(
                "Supabase returned an unexpected logs response",
                code="invalid_provider_response",
            )
        return payload

    async def list_functions(self, project_ref: str) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"/v1/projects/{quote(project_ref, safe='')}/functions",
        )
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise SupabaseManagementError(
                "Supabase returned an unexpected function-list response",
                code="invalid_provider_response",
            )
        return payload

    async def deploy_function(
        self,
        project_ref: str,
        *,
        function_slug: str,
        metadata: bytes,
        source_files: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        multipart: list[tuple[str, tuple[str | None, bytes, str]]] = [
            ("metadata", (None, metadata, "application/json")),
            *[
                ("file", (path, content, "application/octet-stream"))
                for path, content in source_files
            ],
        ]
        payload = await self.request(
            "POST",
            f"/v1/projects/{quote(project_ref, safe='')}/functions/deploy",
            params={"slug": function_slug},
            files=multipart,
            expected_status_codes=(201,),
        )
        if not isinstance(payload, dict):
            raise SupabaseManagementError(
                "Supabase returned an unexpected function response",
                code="invalid_provider_response",
            )
        return payload

    async def delete_function(self, project_ref: str, function_slug: str) -> None:
        await self.request(
            "DELETE",
            (
                f"/v1/projects/{quote(project_ref, safe='')}/functions/"
                f"{quote(function_slug, safe='')}"
            ),
            expected_status_codes=(200,),
        )


__all__ = [
    "SupabaseManagementClient",
    "SupabaseManagementError",
    "validate_supabase_base_url",
]
