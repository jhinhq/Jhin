"""The within-run cognitive graph, Phase 3 minimal form (plan 7.3, ADR-004).

The full plan graph is::

    load_context → reason → {finalize | call_tool | delegate | request_approval}

Phase 3 ships the tool-free path: ``load_context → reason → finalize``. It is
implemented as an explicit, deterministic node sequence rather than a
LangGraph ``StateGraph`` — see ``docs/adr/ADR-004-agent-graph-runtime.md``
for why (Temporal owns durability between steps; a heavyweight graph runtime
inside a single activity would hide state from Temporal). The node names and
event shapes match the plan so a LangGraph swap-in stays possible when tool
loops arrive in Phase 4.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

GraphNode = Literal["load_context", "reason", "finalize"]

# Node order for the Phase 3 tool-free run.
NODE_SEQUENCE: tuple[GraphNode, ...] = ("load_context", "reason", "finalize")


class NodeTransition(BaseModel):
    """Structured event emitted after every meaningful node transition."""

    model_config = ConfigDict(frozen=True)

    node: GraphNode
    detail: str = ""
