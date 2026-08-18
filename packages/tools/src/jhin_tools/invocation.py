"""Versioned deterministic identities for runtime tool invocations."""

from __future__ import annotations

from uuid import UUID, uuid5

TOOL_INVOCATION_FORMAT_VERSION = 1
TOOL_INVOCATION_NAMESPACE = UUID("4f0ac960-eab4-5f17-9b65-9f9bcbf3e0a8")
# Compatibility aliases for code written during the Task 1 implementation.
INVOCATION_FORMAT_VERSION = TOOL_INVOCATION_FORMAT_VERSION
INVOCATION_NAMESPACE = TOOL_INVOCATION_NAMESPACE
MAX_TOOL_CALLS_PER_STEP = 256
MAX_TOOL_STEP_INDEX = 1_000_000


def stable_tool_invocation_id(run_id: UUID, step_index: int, tool_call_ordinal: int) -> UUID:
    """Return the stable claim key for one bounded run/step/call ordinal."""
    if not 0 <= step_index <= MAX_TOOL_STEP_INDEX:
        raise ValueError("tool invocation step index is outside the supported range")
    if not 0 <= tool_call_ordinal < MAX_TOOL_CALLS_PER_STEP:
        raise ValueError("tool invocation call ordinal is outside the supported range")
    return uuid5(
        TOOL_INVOCATION_NAMESPACE,
        f"v1:{run_id.hex}:{step_index}:{tool_call_ordinal}",
    )
