"""Execute one agent reasoning step (plan 7.3).

This runs *inside* a Temporal activity on the agent worker. The caller
supplies a ready :class:`ModelClient` (credentials were resolved at the call
boundary and exist only there); this module never sees provider secrets.

The step is one ``reason`` node: compose messages, call the model once, and
report either a final text answer (``done``) or the structured tool calls
the model requested. Tool authorization and execution happen in the caller
through the tool gateway — never here, and never from model text (plan 52).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from jhin_agents.context import TaskContext, build_messages
from jhin_agents.graph import NodeTransition
from jhin_agents.snapshot import AgentExecutionSnapshot
from jhin_models import ModelClient, ModelRequest, ModelToolCall, ModelUsage, ToolSchema


class StepOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    done: bool
    finish_reason: str
    model: str
    usage: ModelUsage
    latency_ms: int
    provider_request_id: str | None
    transitions: tuple[NodeTransition, ...]
    # Structured tool calls from the provider response — the only channel
    # through which a tool request may enter the gateway (plan 21.4).
    tool_calls: tuple[ModelToolCall, ...] = ()


async def execute_step(
    client: ModelClient,
    snapshot: AgentExecutionSnapshot,
    task: TaskContext,
    tools: tuple[ToolSchema, ...] = (),
    *,
    nudge: str = "",
) -> StepOutcome:
    """load_context (compose messages) then reason (one model call).

    ``nudge`` appends one final instruction message (the empty-completion
    reflective retry passes it with ``tools=()`` to force a plain-language
    reply); the caller owns when to use it.
    """
    messages = build_messages(snapshot, task, has_tools=bool(tools), nudge=nudge)
    transitions = [NodeTransition(node="load_context", detail=f"{len(messages)} messages composed")]

    response = await client.generate(
        ModelRequest(
            model=snapshot.model_profile.model_name,
            messages=messages,
            temperature=snapshot.temperature,
            max_output_tokens=snapshot.max_output_tokens,
            tools=tools,
            web_search=snapshot.model_profile.web_search,
            reasoning=snapshot.model_profile.reasoning,
        )
    )
    transitions.append(
        NodeTransition(
            node="reason",
            detail=f"model {response.model or snapshot.model_profile.model_name} responded",
        )
    )
    if response.tool_calls:
        names = ", ".join(call.name for call in response.tool_calls)
        transitions.append(NodeTransition(node="call_tool", detail=f"requested: {names}"))

    return StepOutcome(
        text=response.text,
        done=not response.tool_calls,
        finish_reason=response.finish_reason,
        model=response.model,
        usage=response.usage,
        latency_ms=response.latency_ms,
        provider_request_id=response.provider_request_id,
        transitions=tuple(transitions),
        tool_calls=response.tool_calls,
    )


def estimate_cost_micros(
    usage: ModelUsage,
    input_cost_micros_per_million: int | None,
    output_cost_micros_per_million: int | None,
) -> int:
    """Integer cost estimate from profile pricing (plan 15.4)."""
    cost = 0
    if input_cost_micros_per_million:
        cost += usage.input_tokens * input_cost_micros_per_million // 1_000_000
    if output_cost_micros_per_million:
        cost += usage.output_tokens * output_cost_micros_per_million // 1_000_000
    return cost
