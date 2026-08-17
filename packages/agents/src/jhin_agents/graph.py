"""The within-run cognitive graph (plan 7.3, ADR-004).

Phase 4 form::

    load_context → reason → {finalize | call_tool | request_approval}
    call_tool → policy_check → {execute_tool → observe → reason
                                | observe(denial) → reason
                                | request_approval → SUSPEND}

It remains an explicit, deterministic state machine rather than a LangGraph
``StateGraph`` — see ``docs/adr/ADR-004-agent-graph-langgraph.md``. Temporal
owns durability *between* reason steps (each reason step is one activity);
the tool branch runs inside the same activity through the tool gateway, and
``request_approval`` surfaces to the workflow, which parks on the
``approval_decision`` signal. Node names and event shapes match the plan so
a LangGraph swap-in stays possible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

GraphNode = Literal[
    "load_context",
    "reason",
    "call_tool",
    "policy_check",
    "execute_tool",
    "observe",
    "request_approval",
    "finalize",
]


class NodeTransition(BaseModel):
    """Structured event emitted after every meaningful node transition."""

    model_config = ConfigDict(frozen=True)

    node: GraphNode
    detail: str = ""
