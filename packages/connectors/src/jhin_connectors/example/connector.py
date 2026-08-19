"""The example :class:`Connector` implementation (plan 11)."""

from __future__ import annotations

from jhin_connectors.base import (
    ConnectionHealth,
    Connector,
    NormalizedEvent,
    RawWebhookEvent,
    VerifyContext,
)
from jhin_connectors.example.manifest import EXAMPLE_MANIFEST
from jhin_connectors.example.tools import EXAMPLE_TOOLS
from jhin_connectors.example.webhook import normalize
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class ExampleConnector(Connector):
    manifest = EXAMPLE_MANIFEST

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        api_key = ctx.credentials.get("api_key", "")
        if not api_key:
            return ConnectionHealth(ok=False, message="api_key is missing")
        return ConnectionHealth(ok=True, message="Example credentials accepted")

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return EXAMPLE_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in EXAMPLE_TOOLS)

    def normalize_event(self, raw: RawWebhookEvent) -> list[NormalizedEvent]:
        return normalize(raw)
