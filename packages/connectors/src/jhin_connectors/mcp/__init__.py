"""Generic Model Context Protocol connector (docs/architecture/mcp.md)."""

from jhin_connectors.mcp.client import (
    McpClient,
    McpConnectionError,
    validate_mcp_server_url,
)
from jhin_connectors.mcp.connector import McpConnector, discover
from jhin_connectors.mcp.discovery import (
    DISCOVERY_KEY,
    OVERRIDES_KEY,
    DiscoveredTool,
    McpToolAnnotations,
    derive_risk,
    effective_risk,
    stored_overrides,
    stored_tools,
    tool_slug,
)
from jhin_connectors.mcp.manifest import MCP_CONNECTOR_TYPE, MCP_MANIFEST
from jhin_connectors.mcp.source import (
    McpToolSource,
    workspace_mcp_connections,
    workspace_mcp_tool_definitions,
)
from jhin_connectors.mcp.tools import (
    McpToolOutput,
    capability_pattern_for,
    connection_tool_definitions,
    connection_tools,
    convert_result,
    tool_name_for,
)

__all__ = [
    "DISCOVERY_KEY",
    "MCP_CONNECTOR_TYPE",
    "MCP_MANIFEST",
    "OVERRIDES_KEY",
    "DiscoveredTool",
    "McpClient",
    "McpConnectionError",
    "McpConnector",
    "McpToolAnnotations",
    "McpToolOutput",
    "McpToolSource",
    "capability_pattern_for",
    "connection_tool_definitions",
    "connection_tools",
    "convert_result",
    "derive_risk",
    "discover",
    "effective_risk",
    "stored_overrides",
    "stored_tools",
    "tool_name_for",
    "tool_slug",
    "validate_mcp_server_url",
    "workspace_mcp_connections",
    "workspace_mcp_tool_definitions",
]
