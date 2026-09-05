"""The "your tools changed" notice (docs/operations/agent-access.md).

An agent that was told "you have no GitHub tool" last turn and is handed one
this turn has no way to know the difference unless somebody says so — the
transcript still carries its own earlier answer, and a model trusts what it
said before. This block is composed from the durable ``agent.step.tools_offered``
event of the previous run in the same conversation, never from anything the
model wrote, so it is exactly as true as the timeline.
"""

from __future__ import annotations

from collections.abc import Iterable

# Beyond this many names the list is summarised as a count: the point is to
# say that something changed, not to restate the catalog.
MAX_LISTED_TOOLS = 20


def _render(names: list[str]) -> str:
    if not names:
        return "none"
    if len(names) > MAX_LISTED_TOOLS:
        return f"{len(names)} tools"
    return ", ".join(names)


def tools_changed_block(previous: Iterable[str], current: Iterable[str]) -> str:
    """The notice, or ``""`` when the two sets are the same."""
    before = set(previous)
    now = set(current)
    if before == now:
        return ""
    added = sorted(now - before)
    removed = sorted(before - now)
    return (
        "Your tools changed since your last reply in this conversation. "
        f"Added: {_render(added)}. Removed: {_render(removed)}. "
        "Do not rely on anything you said about your tools before this turn."
    )


__all__ = ["MAX_LISTED_TOOLS", "tools_changed_block"]
