"""The CLI :class:`Connector` implementation (plan 11.6)."""

from __future__ import annotations

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.cli.manifest import CLI_MANIFEST
from jhin_connectors.cli.tools import CLI_TOOLS
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor


class CliConnector(Connector):
    manifest = CLI_MANIFEST

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        """Configuration-shape check only. The sandbox runner lives on the
        internal ``runner`` network and is reachable from the agent worker,
        not from the API service — so no live probe happens here; a
        misconfigured runner surfaces on the first job instead."""
        if ctx.auth_type != "none":
            return ConnectionHealth(ok=False, message=f"unsupported auth type: {ctx.auth_type!r}")
        network = str(ctx.config.get("default_network") or "none")
        if network not in ("none", "internet"):
            return ConnectionHealth(
                ok=False, message='default_network must be "none" or "internet"'
            )
        image = str(ctx.config.get("default_image") or "")
        details = {"network": network}
        if image:
            details["image"] = image
        return ConnectionHealth(
            ok=True,
            message="Configuration accepted; jobs execute on the sandbox runner",
            details=details,
        )

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return CLI_TOOLS
