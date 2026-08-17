"""The GitHub :class:`Connector` implementation (plan 11.2)."""

from __future__ import annotations

from jhin_connectors.base import (
    ConnectionHealth,
    Connector,
    NormalizedEvent,
    RawWebhookEvent,
    VerifyContext,
)
from jhin_connectors.github.auth import (
    AUTH_GITHUB_APP,
    AUTH_PAT,
    GitHubAuthError,
    build_app_jwt,
    mint_installation_token,
)
from jhin_connectors.github.client import (
    DEFAULT_BASE_URL,
    GitHubApiError,
    github_request,
)
from jhin_connectors.github.manifest import GITHUB_MANIFEST
from jhin_connectors.github.tools import GITHUB_TOOLS
from jhin_connectors.github.webhook import normalize
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class GitHubConnector(Connector):
    manifest = GITHUB_MANIFEST

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        base_url = str(ctx.config.get("base_url") or DEFAULT_BASE_URL)
        try:
            if ctx.auth_type == AUTH_PAT:
                return await self._verify_pat(base_url, ctx.credentials)
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

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        return normalize(raw)
