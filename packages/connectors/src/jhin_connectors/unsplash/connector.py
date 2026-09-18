"""Fixed-origin Unsplash photo picker integration."""

from typing import Any

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.manifest import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorManifest,
    SecretFieldSpec,
)
from jhin_connectors.unsplash.client import API_ORIGIN, verify_access_key
from jhin_connectors.unsplash.tools import (
    AUTONOMOUS_SELECTION_KEY,
    UNSPLASH_TOOLS,
    validator_for,
)
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor, ToolValidator


class UnsplashConnector(Connector):
    manifest = ConnectorManifest(
        connector_type="unsplash",
        display_name="Unsplash",
        icon="image",
        description="Find credited photos for a person's editorial assignment.",
        auth_schemes=(
            AuthSchemeSpec(
                type="api_key",
                label="Access Key",
                secret_fields=(SecretFieldSpec(name="access_key", label="Unsplash Access Key"),),
            ),
        ),
        config_fields=(
            # Declaring the approval here is what confines it to the
            # authenticated admin config route: nothing an agent can call
            # writes a manifest field, and the route's audit row is what
            # later says which person turned it on.
            ConfigFieldSpec(
                name=AUTONOMOUS_SELECTION_KEY,
                label="Let the writer choose the photo",
                help="Off by default: a person's recorded answer picks the cover photo. "
                "Turn this on only with Unsplash's confirmation that automated selection "
                "is allowed for your use. The writer may then pick, but only from photos "
                "its own search for that assignment actually returned.",
                kind="boolean",
                auth_types=("api_key",),
                default=False,
            ),
        ),
        capabilities=tuple(d.name for d, _ in UNSPLASH_TOOLS),
        docs_url="https://unsplash.com/documentation",
    )

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        if config.get("admin_url", API_ORIGIN) != API_ORIGIN:
            raise ValueError("Unsplash uses its fixed official API origin")
        return {**config, "admin_url": API_ORIGIN}

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        """Prove the bound Access Key still works, reading nothing else.

        ``verify_access_key`` makes one bounded read-only search against the
        fixed origin. It touches no download-tracking endpoint, so a health
        check never reports a photo use to Unsplash on nobody's behalf, and
        the provider's own words never reach the message.
        """
        if ctx.auth_type != "api_key":
            return ConnectionHealth(ok=False, message="Unsplash connects with an Access Key")
        if ctx.config.get("admin_url", API_ORIGIN) != API_ORIGIN:
            return ConnectionHealth(ok=False, message="Unsplash uses its fixed official API origin")
        access_key = (ctx.credentials.get("access_key") or "").strip()
        if not access_key:
            return ConnectionHealth(
                ok=False, message="No Unsplash Access Key is bound to this connection"
            )
        ok, message, details = await verify_access_key(access_key)
        return ConnectionHealth(ok=ok, message=message, details=details)

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return UNSPLASH_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(d for d, _ in UNSPLASH_TOOLS)

    def tool_validators(self) -> dict[str, ToolValidator]:
        return {d.name: validator_for(d.name) for d, _ in UNSPLASH_TOOLS if d.defers_scope}
