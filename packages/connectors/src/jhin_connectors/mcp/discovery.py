"""Tool discovery: MCP ``tools/list`` → bounded, durable, risk-labelled tools.

Everything a server reports is untrusted input. Discovery therefore:

- normalizes tool names into capability-safe slugs (``[a-z0-9_]``);
- bounds the number of tools, description length, and schema size;
- derives a :class:`RiskLevel` from the server's annotations
  (``readOnlyHint`` → read, ``destructiveHint`` → destructive, otherwise
  write) and lets admins override it per tool (``tool_risk_overrides``).

The result is persisted in ``connection.config_json["mcp_tools"]`` so the
tool worker can build definitions without talking to the server, and so the
risk an admin reviewed is the risk the gateway enforces.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, ValidationError

from jhin_policy import RiskLevel

DISCOVERY_KEY = "mcp_tools"
DISCOVERED_AT_KEY = "mcp_discovered_at"
OVERRIDES_KEY = "tool_risk_overrides"

MAX_TOOLS = 200
MAX_DESCRIPTION_CHARS = 1_000
MAX_SCHEMA_BYTES = 16_384

SERVER_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")
TOOL_SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9_]+")

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.READ: 0,
    RiskLevel.WRITE: 1,
    RiskLevel.ELEVATED: 2,
    RiskLevel.DESTRUCTIVE: 3,
}


class McpToolAnnotations(BaseModel):
    """The spec's tool annotations, as reported (None = not stated)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None


class DiscoveredTool(BaseModel):
    """One server tool in durable, display-safe form."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    slug: str
    description: str = ""
    input_schema: dict[str, Any] = {}
    schema_truncated: bool = False
    annotations: McpToolAnnotations = McpToolAnnotations()
    derived_risk: RiskLevel


def risk_rank(risk: RiskLevel) -> int:
    return _RISK_ORDER[risk]


def is_valid_server_slug(value: object) -> bool:
    return isinstance(value, str) and bool(SERVER_SLUG_RE.fullmatch(value))


def tool_slug(name: str) -> str | None:
    """Capability-safe slug for a provider tool name (``getIssue`` →
    ``getissue``, ``list-repos`` → ``list_repos``). None when nothing usable
    remains."""
    candidate = _NON_SLUG_RE.sub("_", name.strip().lower()).strip("_")
    candidate = re.sub(r"_+", "_", candidate)[:64]
    return candidate if TOOL_SLUG_RE.fullmatch(candidate) else None


def derive_risk(annotations: McpToolAnnotations) -> RiskLevel:
    """Spec hints → Jhin risk. Absent hints map to ``write`` (the cautious
    default short of requiring approval for every unannotated tool); admins
    can raise or lower it per tool."""
    if annotations.read_only_hint is True:
        return RiskLevel.READ
    if annotations.destructive_hint is True:
        return RiskLevel.DESTRUCTIVE
    return RiskLevel.WRITE


def annotations_from_mcp(raw: mcp_types.ToolAnnotations | None) -> McpToolAnnotations:
    if raw is None:
        return McpToolAnnotations()
    return McpToolAnnotations(
        title=raw.title,
        read_only_hint=raw.readOnlyHint,
        destructive_hint=raw.destructiveHint,
        idempotent_hint=raw.idempotentHint,
        open_world_hint=raw.openWorldHint,
    )


def _bounded_schema(schema: object) -> tuple[dict[str, Any], bool]:
    if not isinstance(schema, dict):
        return {"type": "object"}, True
    try:
        encoded = json.dumps(schema, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"type": "object"}, True
    if len(encoded.encode()) > MAX_SCHEMA_BYTES:
        return {"type": "object"}, True
    return dict(schema), False


def discovered_from_mcp(tools: Sequence[mcp_types.Tool]) -> list[DiscoveredTool]:
    """Convert a ``tools/list`` result. Unusable names and slug collisions are
    dropped (first wins, in server order); the list is capped."""
    result: list[DiscoveredTool] = []
    seen: set[str] = set()
    for tool in tools:
        if len(result) >= MAX_TOOLS:
            break
        slug = tool_slug(tool.name)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        annotations = annotations_from_mcp(tool.annotations)
        schema, truncated = _bounded_schema(tool.inputSchema)
        result.append(
            DiscoveredTool(
                name=tool.name[:200],
                slug=slug,
                description=(tool.description or "")[:MAX_DESCRIPTION_CHARS],
                input_schema=schema,
                schema_truncated=truncated,
                annotations=annotations,
                derived_risk=derive_risk(annotations),
            )
        )
    return result


def discovery_payload(tools: Sequence[DiscoveredTool]) -> dict[str, Any]:
    """The keys merged into ``config_json`` after a successful discovery."""
    return {
        DISCOVERY_KEY: [tool.model_dump(mode="json") for tool in tools],
        DISCOVERED_AT_KEY: datetime.now(UTC).isoformat(),
    }


def stored_tools(config: Mapping[str, Any]) -> list[DiscoveredTool]:
    """Parse the persisted discovery; malformed entries are skipped."""
    raw = config.get(DISCOVERY_KEY)
    if not isinstance(raw, list):
        return []
    tools: list[DiscoveredTool] = []
    seen: set[str] = set()
    for item in raw[:MAX_TOOLS]:
        if not isinstance(item, dict):
            continue
        try:
            tool = DiscoveredTool.model_validate(item)
        except ValidationError:
            continue
        if not TOOL_SLUG_RE.fullmatch(tool.slug) or tool.slug in seen:
            continue
        seen.add(tool.slug)
        tools.append(tool)
    return tools


def stored_overrides(config: Mapping[str, Any]) -> dict[str, RiskLevel]:
    raw = config.get(OVERRIDES_KEY)
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, RiskLevel] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not TOOL_SLUG_RE.fullmatch(key):
            continue
        try:
            overrides[key] = RiskLevel(value)
        except ValueError:
            continue
    return overrides


def effective_risk(tool: DiscoveredTool, overrides: Mapping[str, RiskLevel]) -> RiskLevel:
    return overrides.get(tool.slug, tool.derived_risk)


def discovered_at(config: Mapping[str, Any]) -> str | None:
    value = config.get(DISCOVERED_AT_KEY)
    return value if isinstance(value, str) else None


__all__ = [
    "DISCOVERED_AT_KEY",
    "DISCOVERY_KEY",
    "MAX_DESCRIPTION_CHARS",
    "MAX_SCHEMA_BYTES",
    "MAX_TOOLS",
    "OVERRIDES_KEY",
    "SERVER_SLUG_RE",
    "TOOL_SLUG_RE",
    "DiscoveredTool",
    "McpToolAnnotations",
    "annotations_from_mcp",
    "derive_risk",
    "discovered_at",
    "discovered_from_mcp",
    "discovery_payload",
    "effective_risk",
    "is_valid_server_slug",
    "risk_rank",
    "stored_overrides",
    "stored_tools",
    "tool_slug",
]
