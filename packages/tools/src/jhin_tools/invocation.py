"""Versioned deterministic identities for runtime tool invocations."""

from __future__ import annotations

from uuid import UUID, uuid5

TOOL_INVOCATION_FORMAT_VERSION = 1
TOOL_INVOCATION_NAMESPACE = UUID("4f0ac960-eab4-5f17-9b65-9f9bcbf3e0a8")
SYNC_INVOCATION_FORMAT_VERSION = 1
SYNC_INVOCATION_NAMESPACE = UUID("3dc26b04-1af9-5ec5-a0ea-d7d95c3a393b")
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


def stable_sync_invocation_id(run_id: UUID) -> UUID:
    """Return the stable claim key for one trigger comment-back effect."""
    return uuid5(
        SYNC_INVOCATION_NAMESPACE,
        f"v1:{run_id.hex}:trigger-sync",
    )
