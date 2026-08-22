"""Vercel connector registration and connection lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jhin_connectors.base import (
    ConnectionHealth,
    Connector,
    NormalizedEvent,
    RawWebhookEvent,
    VerifyContext,
)
from jhin_connectors.vercel.client import (
    DEFAULT_BASE_URL,
    VercelApiError,
    VercelClient,
    validate_team_id,
    validate_vercel_base_url,
)
from jhin_connectors.vercel.manifest import VERCEL_MANIFEST
from jhin_connectors.vercel.tools import VERCEL_TOOLS
from jhin_connectors.vercel.webhook import normalize as normalize_webhook_event
from jhin_connectors.vercel.webhook import parse_webhook as parse_vercel_webhook
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class VercelConnector(Connector):
    manifest = VERCEL_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        if auth_type != "access_token":
            raise ValueError("unsupported Vercel auth type")
        normalized = dict(config)
        base_url = normalized.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise ValueError("config field 'base_url' must be text")
        try:
            normalized["base_url"] = validate_vercel_base_url(base_url)
        except VercelApiError:
            raise ValueError("config field 'base_url' is not allowed") from None
        if "team_id" in normalized:
            normalized["team_id"] = validate_team_id(normalized["team_id"])
        return normalized

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        if ctx.auth_type != "access_token":
            return ConnectionHealth(ok=False, message="Unsupported Vercel authentication type")
        token = ctx.credentials.get("token")
        if not isinstance(token, str) or not token:
            return ConnectionHealth(ok=False, message="Vercel access token is missing")
        base_url = ctx.config.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            return ConnectionHealth(ok=False, message="Vercel API target is not allowed")
        team_id = ""
        if "team_id" in ctx.config:
            try:
                team_id = validate_team_id(ctx.config["team_id"])
            except ValueError:
                return ConnectionHealth(ok=False, message="Vercel team configuration is invalid")
        client = VercelClient(base_url=base_url, token=token, team_id=team_id)
        try:
            payload = await client.get_user()
        except VercelApiError as exc:
            return ConnectionHealth(ok=False, message=str(exc))
        user = payload.get("user")
        if not isinstance(user, dict):
            return ConnectionHealth(ok=False, message="Vercel did not return a user")
        username = user.get("username") or user.get("name")
        email = user.get("email") or ""
        if not isinstance(username, str) or not username:
            return ConnectionHealth(ok=False, message="Vercel did not return a username")
        if not isinstance(email, str):
            return ConnectionHealth(ok=False, message="Vercel returned invalid user metadata")
        username = username[:200]
        details = {"username": username, "email": email[:320]}
        if team_id:
            details["team_id"] = team_id
        return ConnectionHealth(
            ok=True,
            message=f"Authenticated as {username}",
            details=details,
        )

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return VERCEL_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in VERCEL_TOOLS)

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> RawWebhookEvent:
        return parse_vercel_webhook(headers, body, secret)

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        return normalize_webhook_event(raw)
