"""Agent runtime for Jhin (plan section 7): snapshots, prompts, run graph."""

from jhin_agents.platform_prompt import (
    PLATFORM_PREAMBLE,
    PLATFORM_PREAMBLE_VERSION,
    render_platform_preamble,
)
from jhin_agents.snapshot import AgentExecutionSnapshot, RunLimits, resolve_snapshot

__all__ = [
    "PLATFORM_PREAMBLE",
    "PLATFORM_PREAMBLE_VERSION",
    "AgentExecutionSnapshot",
    "RunLimits",
    "render_platform_preamble",
    "resolve_snapshot",
]
