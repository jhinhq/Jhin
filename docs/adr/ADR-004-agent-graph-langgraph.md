# ADR-004: Agent execution graph — hand-rolled state machine now, LangGraph later

Status: accepted (Phase 3) — reaffirmed in Phase 4 (see "Phase 4 revisit")

## Context

Plan section 7.3 describes the agent run as a graph: `load_context → reason →
(tools…) → finalize`. Phase 3 ships the minimal tool-free loop only: one
context-composition step and one model call, with strict `max_steps` and
structured events after each node transition.

LangGraph is the plan's intended graph engine. For the Phase 3 loop it would
contribute a `StateGraph` with two nodes and no branching, while adding
`langgraph` + `langchain-core` to the dependency tree of `jhin-agents` and the
agent worker, plus a checkpointer decision we do not need yet (Temporal is the
durability layer; plan 8).

## Decision

Implement the Phase 3 graph as a thin, explicit sequence in
`packages/agents/src/jhin_agents/` (`graph.py` declares the node names and
`NodeTransition` records; `runtime.py` executes `load_context → reason` and
reports transitions). No LangGraph dependency in Phase 3.

The seams LangGraph would occupy are kept deliberately narrow:

- `execute_step()` is the single entry point the Temporal activity calls; its
  contract (snapshot + task context in, `StepOutcome` with transitions and
  usage out) does not change if the interior becomes a LangGraph graph.
- `agent_run.langgraph_thread_id` already exists on the run table (6.13) for
  when a checkpointing graph engine arrives.
- Node names (`load_context`, `reason`, `finalize`) match the plan's graph
  vocabulary, so persisted `run_event` rows stay stable across the swap.

## Consequences

- Phase 3 avoids two heavyweight dependencies for a two-node straight line;
  the loop is fully typed and mypy-strict.
- Phase 4 (tools, branching, approval interrupts) is the natural point to
  adopt LangGraph: conditional edges and tool nodes are where it earns its
  weight. This ADR must be revisited then; if LangGraph is adopted, only
  `jhin-agents` and the agent worker take the dependency (never the API).

## Phase 4 revisit: keep the hand-rolled machine

Phase 4 added the tool branches (`call_tool → policy_check → execute_tool →
observe → reason` and `request_approval → SUSPEND`). We evaluated migrating to
LangGraph at this point, as this ADR required, and decided **against** it.

The deciding observation: after implementation, the branching the plan draws
as an in-graph loop naturally splits across two stronger boundaries —

1. **Between reason steps, Temporal is the graph engine.** Each `reason` step
   is one activity; the workflow loop is the edge back to `reason`. The
   durable interrupt LangGraph would model with a checkpointer + `interrupt()`
   is exactly Temporal's native `wait_condition` on the `approval_decision`
   signal — already proven durable across worker restarts. Running a LangGraph
   checkpointer *inside* an activity would duplicate (and hide from Temporal)
   state that must survive crashes.
2. **Inside a step, the tool path is not a graph, it is a security pipeline.**
   `policy_check → execute_tool` must be the tool gateway's single
   authorization path (plan 12, 52). Expressing it as LangGraph nodes would
   scatter authorization decisions across graph state, making the security
   review surface larger, not smaller.

What we keep from the LangGraph design: `execute_step()` remains the single
entry point; the `StepOutcome` now carries structured `tool_calls`; node names
(`load_context`, `reason`, `call_tool`, `policy_check`, `execute_tool`,
`observe`, `request_approval`, `finalize`) still match plan 7.3, so persisted
`run_event` rows would remain stable under a future swap. The swap trigger is
now: multi-agent delegation subgraphs (plan Phase 7+), where parallel branches
and shared state make a graph runtime genuinely simpler than hand-rolled code.
The API still never imports LangGraph/langchain either way.
