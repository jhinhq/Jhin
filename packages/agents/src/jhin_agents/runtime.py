"""Execute one agent reasoning step (plan 7.3).

This runs *inside* a Temporal activity on the agent worker. The caller
supplies a ready :class:`ModelClient` (credentials were resolved at the call
boundary and exist only there); this module never sees provider secrets.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from jhin_agents.context import TaskContext, build_messages
from jhin_agents.graph import NodeTransition
from jhin_agents.snapshot import AgentExecutionSnapshot
from jhin_models import ModelClient, ModelRequest, ModelUsage


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


async def execute_step(
    client: ModelClient, snapshot: AgentExecutionSnapshot, task: TaskContext
) -> StepOutcome:
    """load_context (compose messages) then reason (one model call)."""
    messages = build_messages(snapshot, task)
    transitions = [NodeTransition(node="load_context", detail=f"{len(messages)} messages composed")]

    response = await client.generate(
        ModelRequest(
            model=snapshot.model_profile.model_name,
            messages=messages,
            temperature=snapshot.temperature,
            max_output_tokens=snapshot.max_output_tokens,
        )
    )
    transitions.append(
        NodeTransition(
            node="reason",
            detail=f"model {response.model or snapshot.model_profile.model_name} responded",
        )
    )

    # Tool-free graph: a single reason step always completes the run.
    return StepOutcome(
        text=response.text,
        done=True,
        finish_reason=response.finish_reason,
        model=response.model,
        usage=response.usage,
        latency_ms=response.latency_ms,
        provider_request_id=response.provider_request_id,
        transitions=tuple(transitions),
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
