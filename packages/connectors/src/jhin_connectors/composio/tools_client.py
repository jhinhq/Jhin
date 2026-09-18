"""Fixed-origin, version-pinned Composio tools REST adapter.

Wire contract: docs.composio.dev/reference/api-reference/tools/getTools and
postToolsExecuteByToolSlug. Discovery uses latest only on its first page;
execution always names a dated version and an already validated account.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from jhin_connectors.composio import ComposioClient, ComposioError
from jhin_connectors.mcp.discovery import MAX_TOOLS

TOOLKIT_RE = re.compile(r"^[a-z0-9_]{1,64}$")
VERSION_RE = re.compile(r"^[0-9]{8}_[0-9]{2,3}$")
TOOL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,199}$")


def valid_toolkit(value: object) -> bool:
    return isinstance(value, str) and TOOLKIT_RE.fullmatch(value) is not None


def pinned_version(value: object) -> str:
    if not isinstance(value, str) or VERSION_RE.fullmatch(value) is None:
        raise ComposioError("Managed app tool has no pinned version; verify the connection")
    return value


def validate_tool(item: dict[str, Any], toolkit: str) -> str:
    owner = item.get("toolkit")
    slug = item.get("slug")
    if (
        not isinstance(owner, dict)
        or owner.get("slug") != toolkit
        or not isinstance(slug, str)
        or TOOL_RE.fullmatch(slug) is None
        or not slug.startswith(f"{toolkit.upper()}_")
    ):
        raise ComposioError("Managed app tool does not match this toolkit")
    return pinned_version(item.get("version"))


class ComposioToolsClient(ComposioClient):
    async def list_tools(self, toolkit: str, *, version: str = "latest") -> list[dict[str, Any]]:
        if not valid_toolkit(toolkit):
            raise ComposioError("Managed app toolkit is invalid")
        selected = version if version == "latest" else pinned_version(version)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        while len(result) < MAX_TOOLS:
            params = {
                "toolkit_slug": toolkit,
                "limit": str(min(50, MAX_TOOLS - len(result))),
                "include_deprecated": "false",
            }
            params[
                "toolkit_versions" if selected == "latest" else f"toolkit_versions[{toolkit}]"
            ] = selected
            if cursor:
                params["cursor"] = cursor
            page = await self._request("GET", f"/tools?{urlencode(params)}")
            items = page.get("items")
            if not isinstance(items, list):
                raise ComposioError("Managed app tool discovery returned an invalid response")
            for item in items[: MAX_TOOLS - len(result)]:
                if not isinstance(item, dict):
                    raise ComposioError("Managed app tool discovery returned an invalid response")
                current = validate_tool(item, toolkit)
                if selected == "latest":
                    selected = current
                if current != selected:
                    raise ComposioError("Managed app tool versions changed during discovery; retry")
                result.append(item)
            next_cursor = page.get("next_cursor")
            if not next_cursor:
                break
            if (
                not isinstance(next_cursor, str)
                or len(next_cursor) > 2048
                or next_cursor in seen
                or not items
            ):
                raise ComposioError("Managed app tool discovery pagination is invalid")
            seen.add(next_cursor)
            cursor = next_cursor
        return result

    async def execute_tool(
        self,
        tool_slug: str,
        *,
        version: str,
        account_id: str,
        user_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if TOOL_RE.fullmatch(tool_slug) is None:
            raise ComposioError("Managed app tool identifier is invalid")
        return await self._request(
            "POST",
            f"/tools/execute/{tool_slug}",
            {
                "connected_account_id": account_id,
                "user_id": user_id,
                "version": pinned_version(version),
                "arguments": arguments,
            },
        )
