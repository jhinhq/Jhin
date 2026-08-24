"""The web :class:`Connector` implementation (docs/architecture/web.md)."""

from __future__ import annotations

from typing import Any

import httpx

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.web.client import (
    backend_base_url,
    build_search_request,
    http_client,
    search_token,
    validate_allowed_domains,
    validate_backend,
)
from jhin_connectors.web.manifest import AUTH_BEARER, WEB_MANIFEST
from jhin_connectors.web.tools import WEB_TOOLS
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class WebConnector(Connector):
    manifest = WEB_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize and policy-check the public settings before persistence."""
        validated = dict(config)
        try:
            validated["allowed_domains"] = validate_allowed_domains(
                validated.get("allowed_domains")
            )
            if not validated["allowed_domains"]:
                validated.pop("allowed_domains", None)
            if auth_type == AUTH_BEARER:
                backend = validate_backend(validated.get("search_backend"))
                validated["search_backend"] = backend
                # Resolves the override through the URL policy (raises on a
                # disallowed origin); the raw override stays stored as given.
                backend_base_url(backend, validated)
            elif validated.get("base_url"):
                # Fetch-only connections may not smuggle in an API base URL.
                validated.pop("base_url", None)
        except EndpointPolicyError:
            raise
        except ValueError as error:
            raise EndpointPolicyError(str(error)) from None
        return validated

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        if ctx.auth_type != AUTH_BEARER:
            try:
                validate_allowed_domains(ctx.config.get("allowed_domains"))
            except ValueError as error:
                return ConnectionHealth(ok=False, message=str(error))
            return ConnectionHealth(
                ok=True,
                message="Fetch-only connection; URL policy checks passed",
                details={"mode": "fetch_only"},
            )
        try:
            backend = validate_backend(ctx.config.get("search_backend"))
            base_url = backend_base_url(backend, ctx.config)
            token = search_token(ctx.credentials)
        except (EndpointPolicyError, ValueError) as error:
            return ConnectionHealth(ok=False, message=str(error))
        spec = build_search_request(backend, base_url, token, "connectivity check", 1)
        try:
            async with http_client(spec.headers) as client:
                response = await client.request(
                    spec.method, spec.url, params=spec.params or None, json=spec.json_body
                )
        except httpx.HTTPError as error:
            return ConnectionHealth(
                ok=False,
                message=f"Could not reach the {backend} search API ({type(error).__name__})",
            )
        if response.status_code in (401, 403):
            return ConnectionHealth(
                ok=False,
                message=f"The {backend} search API rejected the key (HTTP {response.status_code})",
            )
        if response.status_code >= 400:
            return ConnectionHealth(
                ok=False,
                message=f"The {backend} search API answered HTTP {response.status_code}",
            )
        return ConnectionHealth(
            ok=True,
            message=f"The {backend} search API answered HTTP {response.status_code}",
            details={"backend": backend, "status": str(response.status_code)},
        )

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return WEB_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in WEB_TOOLS)
