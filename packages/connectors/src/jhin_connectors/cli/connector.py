"""The CLI :class:`Connector` implementation (plan 11.6)."""

from __future__ import annotations

from collections.abc import Mapping

from jhin_connectors.base import ConnectionHealth, Connector, VerifyContext
from jhin_connectors.cli.manifest import CLI_MANIFEST
from jhin_connectors.cli.tools import CLI_TOOLS
from jhin_connectors.cli.validators import (
    command_network_validator,
    repository_allow_list_validator,
    workspace_repository_validator,
)
from jhin_policy import ToolDefinition
from jhin_tools.builtin import ToolExecutor, ToolValidator


class CliConnector(Connector):
    manifest = CLI_MANIFEST

    async def verify_connection(self, ctx: VerifyContext) -> ConnectionHealth:
        """Configuration-shape check only. The sandbox runner lives on the
        internal ``runner`` network and is reachable from the tool worker,
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
        repositories = ctx.config.get("allowed_repositories") or []
        details = {"network": network}
        if image:
            details["image"] = image
        details["repositories"] = (
            ", ".join(str(item) for item in repositories) if repositories else "none"
        )
        message = "Configuration accepted; jobs execute on the sandbox runner"
        if not repositories:
            message += ". No repositories are allowed yet, so checkout and push are refused"
        return ConnectionHealth(ok=True, message=message, details=details)

    def tools(self) -> tuple[tuple[ToolDefinition, ToolExecutor], ...]:
        return CLI_TOOLS

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition for definition, _executor in CLI_TOOLS)

    def tool_validators(self) -> Mapping[str, ToolValidator]:
        """The repository allow-list is a per-call veto, not a grant scope:
        it must hold for every agent in the workspace, and it must re-run when
        a parked approval resumes.

        Two shapes of the same rule. Checkout and push *name* a repository, so
        the name is what is checked. Every other sandbox tool names none — and
        with a durable workspace they read whatever the last checkout left in
        ``/workspace/repo``, which is how a repository removed from the list
        stayed readable for as long as the disk did. Those are checked against
        what the workspace actually holds.
        """
        return {
            "cli.repository.checkout": repository_allow_list_validator,
            "cli.repository.push": repository_allow_list_validator,
            "cli.command.execute": command_network_validator,
            "cli.test.run": workspace_repository_validator,
            "cli.file.read": workspace_repository_validator,
            "cli.file.publish": workspace_repository_validator,
            "cli.file.write": workspace_repository_validator,
            "cli.file.edit": workspace_repository_validator,
            "cli.file.list": workspace_repository_validator,
            "cli.file.search": workspace_repository_validator,
        }
