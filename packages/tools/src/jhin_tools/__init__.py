"""jhin-tools: the tool gateway — the single authorization path for every
agent tool call (plan section 12) — plus the Phase 4 built-in system tools."""

from jhin_tools.builtin import (
    BUILTIN_TOOLS,
    ToolCatalog,
    ToolExecutionContext,
    allowed_tool_definitions,
    build_builtin_catalog,
)
from jhin_tools.errors import ToolExecutionError
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
from jhin_tools.test_barriers import (
    AGENT_BEFORE_BIND,
    PHASE9_AFTER_MANIFEST,
    PHASE9_CLEANUP_BEFORE_EFFECT,
    PHASE9_SYNC_BEFORE_EFFECT,
    TOOL_AFTER_CLAIM,
    TOOL_AFTER_EFFECT,
    TOOL_BEFORE_CLAIM,
    CrashBarrier,
    CrashBarrierConfig,
    CrashBarrierName,
    release_barrier,
)

__all__ = [
    "AGENT_BEFORE_BIND",
    "BUILTIN_TOOLS",
    "INVOCATION_FORMAT_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_STRING_CHARS",
    "MAX_TOOL_CALLS_PER_STEP",
    "MAX_TOOL_STEP_INDEX",
    "PHASE9_AFTER_MANIFEST",
    "PHASE9_CLEANUP_BEFORE_EFFECT",
    "PHASE9_SYNC_BEFORE_EFFECT",
    "TOOL_AFTER_CLAIM",
    "TOOL_AFTER_EFFECT",
    "TOOL_BEFORE_CLAIM",
    "TOOL_INVOCATION_FORMAT_VERSION",
    "TOOL_INVOCATION_NAMESPACE",
    "CrashBarrier",
    "CrashBarrierConfig",
    "CrashBarrierName",
    "GatewayOutcome",
    "GatewayStateError",
    "ToolCatalog",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolGateway",
    "allowed_tool_definitions",
    "build_builtin_catalog",
    "release_barrier",
    "sanitize_payload",
    "stable_tool_invocation_id",
]
