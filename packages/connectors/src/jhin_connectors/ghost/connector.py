"""Native Ghost connector. All edits are drafts; publication has its own gate."""

from typing import Any
from uuid import UUID

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.ghost.archive import ARCHIVE_TOOLS
from jhin_connectors.ghost.assignments import ASSIGNMENT_TOOLS
from jhin_connectors.ghost.client import GhostApiError, ghost_request, validate_admin_url
from jhin_connectors.ghost.manifest import GHOST_MANIFEST
from jhin_connectors.ghost.setup import GHOST_SETUP_TOOLS
from jhin_connectors.ghost.tools import GHOST_TOOLS
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor, ToolValidator


class GhostConnector(Connector):
    manifest = GHOST_MANIFEST

    def validate_settings(self, auth_type: str, config: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(config)
        try:
            normalized["admin_url"] = validate_admin_url(str(config.get("admin_url", "")))
            publisher = config.get("publisher_agent_id")
            if publisher:
                normalized["publisher_agent_id"] = str(UUID(str(publisher)))
        except (GhostApiError, ValueError):
            raise ValueError("Supply a valid Ghost Admin URL and publishing agent ID") from None
        return normalized

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        try:
            await ghost_request(
                str(ctx.config.get("admin_url", "")),
                ctx.credentials.get("admin_key", ""),
                "GET",
                "posts/",
                params={"limit": 1},
            )
        except GhostApiError as error:
            return ConnectionHealth(ok=False, message=str(error))
        return ConnectionHealth(
            ok=True, message="Ghost Admin API connected", details={"auth": "admin_integration"}
        )

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return (*GHOST_TOOLS, *GHOST_SETUP_TOOLS, *ASSIGNMENT_TOOLS, *ARCHIVE_TOOLS)

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in self.tools())

    def tool_validators(self) -> dict[str, ToolValidator]:
        from jhin_connectors.ghost.access import validator_for

        return {
            definition.name: validator_for(definition.name)
            for definition, _ in (*GHOST_TOOLS, *ASSIGNMENT_TOOLS, *ARCHIVE_TOOLS)
        }
