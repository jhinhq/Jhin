"""The Linear :class:`Connector` implementation (plan 11.3)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jhin_connectors.base import (
    ConnectionHealth,
    Connector,
    NormalizedEvent,
    RawWebhookEvent,
    VerifyContext,
    WebhookVerificationError,
)
from jhin_connectors.linear.client import (
    AUTH_API_KEY,
    AUTH_OAUTH,
    DEFAULT_BASE_URL,
    LinearApiError,
    linear_graphql,
    validate_linear_base_url,
)
from jhin_connectors.linear.manifest import LINEAR_MANIFEST
from jhin_connectors.linear.tools import LINEAR_TOOLS, TEAMS_QUERY
from jhin_connectors.linear.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    normalize,
    parse_payload,
    timestamp_is_fresh,
    verify_signature,
)
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor

_VIEWER_QUERY = "query { viewer { id name email } }"


class LinearConnector(Connector):
    manifest = LINEAR_MANIFEST

    def validate_settings(self, _auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize and approve the public API origin before persistence."""
        normalized = dict(config)
        base_url = normalized.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise ValueError("config field 'base_url' must be text")
        try:
            normalized["base_url"] = validate_linear_base_url(base_url)
        except LinearApiError:
            raise ValueError("config field 'base_url' is not allowed") from None
        return normalized

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        if ctx.auth_type == AUTH_OAUTH:
            # Declared in the manifest for forward compatibility; the token
            # exchange/refresh flow is not implemented yet (plan 11.3).
            return ConnectionHealth(
                ok=False,
                message="OAuth authentication is not implemented yet; use a personal API key.",
            )
        if ctx.auth_type != AUTH_API_KEY:
            return ConnectionHealth(ok=False, message=f"unsupported auth type: {ctx.auth_type!r}")
        api_key = ctx.credentials.get("api_key", "")
        if not api_key:
            return ConnectionHealth(ok=False, message="api_key is missing")
        base_url = str(ctx.config.get("base_url") or DEFAULT_BASE_URL)
        try:
            data = await linear_graphql(base_url, api_key, _VIEWER_QUERY)
        except LinearApiError as exc:
            return ConnectionHealth(ok=False, message=str(exc))
        viewer = data.get("viewer")
        if not isinstance(viewer, dict):
            return ConnectionHealth(ok=False, message="Linear did not return a viewer")
        name = str(viewer.get("name") or viewer.get("email") or "unknown")
        return ConnectionHealth(
            ok=True,
            message=f"Authenticated as {name}",
            details={"viewer": name, "auth": "api_key"},
        )

    async def fetch_metadata(self, ctx: VerifyContext) -> dict[str, Any]:
        """Teams and workflow states for UI pickers (trigger builder)."""
        api_key = ctx.credentials.get("api_key", "")
        base_url = str(ctx.config.get("base_url") or DEFAULT_BASE_URL)
        data = await linear_graphql(base_url, api_key, TEAMS_QUERY)
        teams: list[dict[str, Any]] = []
        raw_teams = data.get("teams")
        nodes = raw_teams.get("nodes", []) if isinstance(raw_teams, dict) else []
        for team in nodes:
            if not isinstance(team, dict):
                continue
            raw_states = team.get("states")
            state_nodes = raw_states.get("nodes", []) if isinstance(raw_states, dict) else []
            teams.append(
                {
                    "id": str(team.get("id", "")),
                    "key": str(team.get("key", "")),
                    "name": str(team.get("name", "")),
                    "states": [
                        {
                            "id": str(state.get("id", "")),
                            "name": str(state.get("name", "")),
                            "type": str(state.get("type", "")),
                        }
                        for state in state_nodes
                        if isinstance(state, dict)
                    ],
                }
            )
        return {"teams": teams}

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return LINEAR_TOOLS

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> RawWebhookEvent:
        # Signature check comes strictly first (plan 48.5): the body is not
        # even JSON-parsed until HMAC verification succeeds. Linear sends a
        # bare hex digest (no sha256= prefix).
        if not verify_signature(secret, body, headers.get(SIGNATURE_HEADER)):
            raise WebhookVerificationError(f"invalid or missing {SIGNATURE_HEADER} signature")
        event = headers.get(EVENT_HEADER, "")
        delivery_id = headers.get(DELIVERY_HEADER, "")
        if not event or not delivery_id:
            raise WebhookVerificationError(f"missing {EVENT_HEADER} or {DELIVERY_HEADER} header")
        try:
            payload = parse_payload(body)
        except ValueError as exc:
            raise WebhookVerificationError(f"invalid payload: {exc}") from None
        # Replay guard (Linear's documented recommendation): the payload's
        # webhookTimestamp (Unix ms) must be within a minute of now.
        if not timestamp_is_fresh(payload):
            raise WebhookVerificationError("webhookTimestamp is missing or outside the tolerance")
        return RawWebhookEvent(event=event, delivery_id=delivery_id, payload=payload)

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        return normalize(raw)
