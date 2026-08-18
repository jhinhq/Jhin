"""jhin-tools: the tool gateway — the single authorization path for every
agent tool call (plan section 12) — plus the Phase 4 built-in system tools."""

from jhin_tools.builtin import (
    BUILTIN_TOOLS,
    ToolCatalog,
    ToolExecutionContext,
    allowed_tool_definitions,
    build_builtin_catalog,
)
from jhin_tools.gateway import (
    GatewayOutcome,
    GatewayStateError,
    ToolGateway,
)
from jhin_tools.invocation import (
    INVOCATION_FORMAT_VERSION,
    MAX_TOOL_CALLS_PER_STEP,
    MAX_TOOL_STEP_INDEX,
    TOOL_INVOCATION_FORMAT_VERSION,
    TOOL_INVOCATION_NAMESPACE,
    stable_tool_invocation_id,
)
from jhin_tools.sanitize import (
    MAX_DOCUMENT_BYTES,
    MAX_STRING_CHARS,
    sanitize_payload,
)

__all__ = [
    "BUILTIN_TOOLS",
    "INVOCATION_FORMAT_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_STRING_CHARS",
    "MAX_TOOL_CALLS_PER_STEP",
    "MAX_TOOL_STEP_INDEX",
    "TOOL_INVOCATION_FORMAT_VERSION",
    "TOOL_INVOCATION_NAMESPACE",
    "GatewayOutcome",
    "GatewayStateError",
    "ToolCatalog",
    "ToolExecutionContext",
    "ToolGateway",
    "allowed_tool_definitions",
    "build_builtin_catalog",
    "sanitize_payload",
    "stable_tool_invocation_id",
]
