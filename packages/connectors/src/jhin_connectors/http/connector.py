"""The generic HTTP :class:`Connector` implementation (plan 11.7)."""

from __future__ import annotations

from typing import Any

import httpx

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.http.client import (
    auth_headers,
    default_headers_from_config,
    http_client,
    send_bounded_text,
    validate_http_base_url,
    validate_request_header_name,
)
from jhin_connectors.http.manifest import AUTH_HEADER, HTTP_MANIFEST
from jhin_connectors.http.tools import HTTP_TOOLS
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class HttpConnector(Connector):
    manifest = HTTP_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize and policy-check the config before persistence."""
        validated = dict(config)
        try:
            validated["base_url"] = validate_http_base_url(str(config.get("base_url") or ""))
            default_headers_from_config(validated)
            if auth_type == AUTH_HEADER:
                validate_request_header_name(validated.get("header_name"))
        except EndpointPolicyError:
            raise
        except ValueError as error:
            raise EndpointPolicyError(str(error)) from None
        return validated

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        try:
            base_url = validate_http_base_url(str(ctx.config.get("base_url") or ""))
            headers = default_headers_from_config(ctx.config)
            headers.update(auth_headers(ctx.auth_type, ctx.credentials, ctx.config))
        except (EndpointPolicyError, ValueError) as error:
            return ConnectionHealth(ok=False, message=str(error))
        failures: list[str] = []
        async with http_client(headers) as client:
            for method in ("HEAD", "GET"):
                try:
                    request = client.build_request(method, base_url)
                    status, _content_type, _text, _truncated = await send_bounded_text(
                        client, request
                    )
                except httpx.HTTPError as error:
                    failures.append(type(error).__name__)
                    continue
                if method == "HEAD" and status in (405, 501):
                    continue  # servers without HEAD support get one GET probe
                if status < 500:
                    return ConnectionHealth(
                        ok=True,
                        message=f"Base URL answered HTTP {status}",
                        details={"status": str(status)},
                    )
                return ConnectionHealth(ok=False, message=f"The server answered HTTP {status}")
        detail = ", ".join(sorted(set(failures))) or "no response"
        return ConnectionHealth(ok=False, message=f"Could not reach the base URL ({detail})")

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return HTTP_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in HTTP_TOOLS)
