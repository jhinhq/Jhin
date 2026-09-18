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

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from jhin_agents.context import TaskContext, build_messages
from jhin_agents.context_budget import budget_context
from jhin_agents.graph import NodeTransition
from jhin_agents.snapshot import AgentExecutionSnapshot
from jhin_models import (
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelStreamEvent,
    ModelToolCall,
    ModelUsage,
    ToolSchema,
)
from jhin_models.providers.ollama import measured_context_window, serving_window_options


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
    on_event: Callable[[ModelStreamEvent], Awaitable[None]] | None = None,
) -> StepOutcome:
    """load_context (compose messages) then reason (one model call).

    ``nudge`` appends one final instruction message. The empty-completion
    retry retains the same tools and asks the model to continue the task;
    the caller owns when to use it.
    """
    messages = build_messages(snapshot, task, has_tools=bool(tools), nudge=nudge)
    if snapshot.model_profile.supports_images is False and any(
        part.type == "image" for message in messages for part in message.content_parts
    ):
        raise ModelProviderError(
            "The selected model profile does not support image inputs. "
            "Choose an image-capable model.",
            retryable=False,
            error_code="unsupported_image_input",
        )
    # Ask the server what window it is already serving before budgeting
    # against the one this step will ask for. The request is honoured in the
    # ordinary case, but a host that answered an earlier ask with less — no
    # memory for more, its own ceiling, an instance loaded by someone else —
    # is the one telling the truth, and only the host knows. Nothing
    # measurable means the budget falls back to the request, never above it.
    served_window = await measured_context_window(
        client,
        provider_type=snapshot.model_profile.provider_type,
        model=snapshot.model_profile.model_name,
    )
    budget = budget_context(
        messages,
        tools,
        provider_type=snapshot.model_profile.provider_type,
        context_window=snapshot.model_profile.context_window,
        max_output_tokens=snapshot.max_output_tokens,
        served_context_window=served_window,
    )
    messages = budget.messages
    detail = f"{len(messages)} messages composed"
    if budget.estimated_input_tokens is not None:
        detail += (
            f"; estimated input {budget.estimated_input_tokens}/{budget.input_token_limit} tokens"
            f"; {budget.shortened_messages} history excerpts shortened"
            # Says which, so a reader never has to assume the window was
            # verified when it was only assumed.
            f"; context window {budget.context_window} ({budget.context_window_source})"
        )
        if budget.requested_context_window is not None:
            # The number actually sent, which a measurement may have clamped
            # the admitted window below. Without it a refusal cannot be told
            # apart from a request the host quietly served smaller.
            detail += f"; requested num_ctx {budget.requested_context_window}"
    transitions = [NodeTransition(node="load_context", detail=detail)]

    request = ModelRequest(
        model=snapshot.model_profile.model_name,
        messages=messages,
        temperature=snapshot.temperature,
        max_output_tokens=budget.max_output_tokens,
        tools=tools,
        web_search=snapshot.model_profile.web_search,
        reasoning=snapshot.model_profile.reasoning,
        # Ask for the window the prompt was just budgeted against, so the
        # instance the provider loads is the size this budget assumed. Empty
        # for a provider whose window Jhin cannot set.
        extra=serving_window_options(budget.requested_context_window),
    )
    if on_event is None:
        response = await client.generate(request)
    else:
        response = None
        async for event in client.stream_events(request):
            await on_event(event)
            if event.type == "completed":
                response = event.response
    if response is None:
        raise ModelProviderError("Provider stream did not complete", retryable=True)
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
