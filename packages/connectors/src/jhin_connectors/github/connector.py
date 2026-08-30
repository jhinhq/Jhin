"""The GitHub :class:`Connector` implementation (plan 11.2)."""

from __future__ import annotations

import json
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
from jhin_connectors.github.auth import (
    AUTH_DEVICE,
    AUTH_GITHUB_APP,
    AUTH_OAUTH,
    AUTH_PAT,
    GitHubAuthError,
    build_app_jwt,
    mint_installation_token,
    oauth_access_token,
)
from jhin_connectors.github.client import (
    DEFAULT_BASE_URL,
    GitHubApiError,
    github_request,
    validate_github_base_url,
)
from jhin_connectors.github.manifest import GITHUB_MANIFEST
from jhin_connectors.github.tools import GITHUB_TOOLS
from jhin_connectors.github.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    normalize,
    verify_signature,
)
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class GitHubConnector(Connector):
    manifest = GITHUB_MANIFEST

    def validate_settings(self, _auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize and approve the public API origin before persistence."""
        normalized = dict(config)
        base_url = normalized.get("base_url", DEFAULT_BASE_URL)
        if not isinstance(base_url, str):
            raise ValueError("config field 'base_url' must be text")
        try:
            normalized["base_url"] = validate_github_base_url(base_url)
        except GitHubApiError:
            raise ValueError("config field 'base_url' is not allowed") from None
        return normalized

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        base_url = str(ctx.config.get("base_url") or DEFAULT_BASE_URL)
        try:
            if ctx.auth_type == AUTH_PAT:
                return await self._verify_pat(base_url, ctx.credentials)
            if ctx.auth_type in (AUTH_OAUTH, AUTH_DEVICE):
                return await self._verify_oauth(ctx.auth_type, base_url, ctx.credentials)
            if ctx.auth_type == AUTH_GITHUB_APP:
                return await self._verify_app(base_url, ctx.credentials)
        except (GitHubApiError, GitHubAuthError) as exc:
            return ConnectionHealth(ok=False, message=str(exc))
        return ConnectionHealth(ok=False, message=f"unsupported auth type: {ctx.auth_type!r}")

    async def _verify_pat(self, base_url: str, credentials: dict[str, str]) -> ConnectionHealth:
        token = credentials.get("token", "")
        if not token:
            return ConnectionHealth(ok=False, message="token is missing")
        user = await github_request("GET", base_url, "/user", token)
        login = str(user.get("login", "unknown"))
        return ConnectionHealth(
            ok=True,
            message=f"Authenticated as {login}",
            details={"login": login, "auth": "pat"},
        )

    async def _verify_oauth(
        self, auth_type: str, base_url: str, credentials: dict[str, str]
    ) -> ConnectionHealth:
        """One live call with the token the sign-in produced: who is this?

        Deliberately the same check as a PAT — the token is a bearer token
        whichever way it was obtained — so a connection made in the browser
        and one made with a device code prove themselves identically.
        """
        user = await github_request("GET", base_url, "/user", oauth_access_token(credentials))
        login = str(user.get("login", "unknown"))
        return ConnectionHealth(
            ok=True,
            message=f"Authenticated as {login}",
            details={"login": login, "auth": auth_type},
        )

    async def _verify_app(self, base_url: str, credentials: dict[str, str]) -> ConnectionHealth:
        # Two live checks: the app JWT is accepted (GET /app) and an
        # installation token can actually be minted for the installation id.
        app_jwt = build_app_jwt(credentials)
        app = await github_request("GET", base_url, "/app", app_jwt)
        await mint_installation_token(base_url, credentials)
        slug = str(app.get("slug", "") or app.get("name", "unknown"))
        return ConnectionHealth(
            ok=True,
            message=f"GitHub App '{slug}' installation token minted",
            details={"app": slug, "auth": "github_app"},
        )

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return GITHUB_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in GITHUB_TOOLS)

    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> RawWebhookEvent:
        # Signature check comes strictly first (plan 48.5): the body is not
        # even JSON-parsed until HMAC verification succeeds.
        if not verify_signature(secret, body, headers.get(SIGNATURE_HEADER)):
            raise WebhookVerificationError(f"invalid or missing {SIGNATURE_HEADER} signature")
        event = headers.get(EVENT_HEADER, "")
        delivery_id = headers.get(DELIVERY_HEADER, "")
        if not event or not delivery_id:
            raise WebhookVerificationError(f"missing {EVENT_HEADER} or {DELIVERY_HEADER} header")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise WebhookVerificationError("payload is not valid JSON") from None
        if not isinstance(payload, dict):
            raise WebhookVerificationError("payload must be a JSON object")
        return RawWebhookEvent(event=event, delivery_id=delivery_id, payload=payload)

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        return normalize(raw)
