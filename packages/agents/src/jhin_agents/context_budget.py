"""Deterministic prompt budgeting; persisted history is never changed.

Provider tokenizers differ. The byte-based estimate deliberately reserves more
than the usual English characters/token heuristic, plus a separate template
margin. It is an admission estimate, not an exact provider token count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from jhin_agents.context import UNTRUSTED_LABEL
from jhin_models import ModelMessage, ModelProviderError, ToolSchema
from jhin_models.providers.ollama import requested_serving_window
from jhin_models.tool_schemas import inline_local_schema_refs

# What a local Ollama host is assumed to be serving when the profile names no
# window and nothing could be measured: Ollama 0.34's own default. It is an
# assumption only — nothing is asked for on its strength, because ``num_ctx``
# is how much KV cache the instance allocates and a host running larger by its
# own configuration must not be shrunk to it. A bigger window is a per-profile
# decision, taken by setting the profile's context window.
DEFAULT_OLLAMA_CONTEXT_WINDOW = 32_768
DEFAULT_OUTPUT_TOKENS = 4096
# Providers whose serving window Jhin sets and then has to respect. Ollama
# allocates exactly the window its instance was loaded with, and a run that
# overruns it loses the oldest part of the prompt to context shifting instead
# of being refused — a silent loss, of whatever standing instruction happens
# to be furthest back. A hosted API answers an oversized request with an
# error, so it needs no cap of Jhin's and keeps its own output default.
SELF_ENFORCED_WINDOW_PROVIDERS = frozenset({"ollama"})


@dataclass(frozen=True)
class BudgetedContext:
    messages: tuple[ModelMessage, ...]
    max_output_tokens: int | None
    estimated_input_tokens: int | None = None
    input_token_limit: int | None = None
    shortened_messages: int = 0
    # The window this request was admitted against. None means nobody knows,
    # so nothing was budgeted.
    context_window: int | None = None
    # Where that number came from: "measured" (the server reported serving
    # it), "profile" (the profile's configured window, which is what Jhin
    # asked the server for), or "assumed" (Ollama's documented default, which
    # nothing asked for and nothing confirmed). Carried so the step record can
    # say which, instead of implying the window was verified — or asked for.
    context_window_source: str | None = None
    # The window to ask the provider to serve, for a provider that lets Jhin
    # choose (:data:`SELF_ENFORCED_WINDOW_PROVIDERS`). None for the rest:
    # nothing to ask, the API serves what it serves.
    requested_context_window: int | None = None


def requested_context_window(*, provider_type: str, context_window: int | None) -> int | None:
    """The window Jhin asks a self-enforced provider to serve.

    For Ollama this becomes ``options.num_ctx``, which decides how large an
    instance the server loads. Only an explicitly configured profile window is
    asked for; the rule itself lives beside the wire spelling in
    :func:`jhin_models.providers.ollama.requested_serving_window`, because
    every Jhin path that reaches one Ollama profile — an agent step, memory
    extraction, dedup adjudication — has to ask for the same window or the
    next request reloads the instance at a different size.
    """
    return requested_serving_window(provider_type=provider_type, context_window=context_window)


def resolve_context_window(
    *, provider_type: str, context_window: int | None, served_context_window: int | None
) -> tuple[int | None, str | None]:
    """The smallest credible window, and an honest word for where it came from.

    Three different claims, never conflated by the word they are reported
    under. ``"profile"`` is a window Jhin *asked* for: the profile configured
    it, so Jhin sends it as ``num_ctx`` and the instance is loaded at that size
    — the profile's number rather than the architecture's maximum precisely
    because Jhin now sends it. ``"measured"`` is a window the host reported it
    is really serving (``/api/ps``). ``"assumed"`` is neither: nothing was
    asked for and nothing was measured, so the budget falls back to
    :data:`DEFAULT_OLLAMA_CONTEXT_WINDOW`, Ollama's own default, and says so.

    A measurement clamps a request downward, never upward: a server that
    answered a request for more with less — no memory, its own ceiling, an
    instance someone else loaded first — is the one telling the truth. But the
    assumption is a fallback, not a clamp. A deployment that configured no
    window asks for nothing, so a host measured serving 131,072 is serving
    131,072, and budgeting it down to the default would refuse prompts that fit.
    """
    candidates: list[tuple[int, str]] = []
    if served_context_window is not None and served_context_window > 0:
        candidates.append((served_context_window, "measured"))
    if context_window is not None and context_window > 0:
        candidates.append((context_window, "profile"))
    if not candidates and provider_type in SELF_ENFORCED_WINDOW_PROVIDERS:
        candidates.append((DEFAULT_OLLAMA_CONTEXT_WINDOW, "assumed"))
    if candidates:
        window = min(size for size, _ in candidates)
        # Measured is listed first, so a tie is attributed to the measurement.
        return window, next(source for size, source in candidates if size == window)
    return None, None


def _text_tokens(text: str) -> int:
    # Three ASCII bytes per token, measured rather than assumed: against
    # qwen3.8's own tokenizer, 20 KB of dense tool-schema JSON came back at
    # 4.88 bytes/token and English prose at 4.92, so three keeps about 1.6x
    # headroom on both. Two bytes/token -- the earlier guess -- overstated a
    # real prompt by 2.45x, which refused prompts that fit the window with
    # room to spare; an estimate wrong in that direction is not "safe", it
    # just fails closed on work that would have succeeded.
    # Non-ASCII still counts one UTF-8 byte per token, avoiding the severe
    # undercount of characters/4 for CJK, emoji and arbitrary data.
    encoded = text.encode("utf-8")
    ascii_bytes = sum(byte < 128 for byte in encoded)
    return (ascii_bytes + 2) // 3 + len(encoded) - ascii_bytes


def _message_tokens(message: ModelMessage) -> int:
    wire = message.model_dump(mode="json", exclude={"content_parts"}, exclude_none=True)
    cost = 32 + _text_tokens(json.dumps(wire, ensure_ascii=False, separators=(",", ":")))
    for part in message.content_parts:
        # Images are not their base64 token length. Reserve conservatively;
        # neither the image nor attached human-provided text is discarded.
        cost += 16_384 if part.type == "image" else _text_tokens(part.text)
    return cost


def _tool_tokens(tools: tuple[ToolSchema, ...], *, provider_type: str) -> int:
    total = 128  # provider template / request framing, before the wider margin
    for tool in tools:
        parameters = (
            inline_local_schema_refs(tool.parameters)
            if provider_type == "ollama"
            else tool.parameters
        )
        wire = {
            "type": "function",
            "function": {
                "name": tool.name.replace(".", "__"),
                "description": tool.description,
                "parameters": parameters,
            },
        }
        total += 32 + _text_tokens(json.dumps(wire, ensure_ascii=False, separators=(",", ":")))
    return total


def _shortened_content(message: ModelMessage, *, excerpt_bytes: int) -> str:
    body = message.content
    if message.role == "tool":
        body = body.removeprefix(UNTRUSTED_LABEL)
    encoded = body.encode("utf-8")
    head_bytes = excerpt_bytes * 3 // 4
    excerpt = (
        encoded[:head_bytes].decode("utf-8", errors="ignore")
        + "\n[…]\n"
        + encoded[-(excerpt_bytes - head_bytes) :].decode("utf-8", errors="ignore")
    )
    if message.role == "tool":
        return (
            UNTRUSTED_LABEL
            + f"[Context excerpt is incomplete. Full result remains in the persisted task "
            f"transcript for tool_call_id={message.tool_call_id}. Retrieve needed evidence "
            "with available read tools; do not infer omitted facts.]\n" + excerpt
        )
    return (
        "[Earlier assistant text shortened for context. Full text remains in the persisted "
        "task transcript; this excerpt is incomplete.]\n" + excerpt
    )


def budget_context(
    messages: tuple[ModelMessage, ...],
    tools: tuple[ToolSchema, ...],
    *,
    provider_type: str,
    context_window: int | None,
    max_output_tokens: int | None,
    served_context_window: int | None = None,
) -> BudgetedContext:
    """Leave response room without dropping authority or breaking tool pairs.

    The window admitted against is the smallest credible one
    (:func:`resolve_context_window`): the profile's configured window, which
    for a self-enforced provider is also what Jhin asks the server to serve,
    and whatever the server reports actually serving, which can only clamp
    that downward; failing both, :data:`DEFAULT_OLLAMA_CONTEXT_WINDOW` as a
    named assumption. The result carries the requested window as well, because
    the caller has to put it in the request
    (:func:`jhin_models.providers.ollama.serving_window_options`) for the
    number budgeted against to be the number served — and ``None`` there means
    the request pins nothing, which is what leaves a host its own window.

    With no window at all, nothing is budgeted and nothing is invented: the
    messages pass through and ``max_output_tokens`` stays exactly as
    configured, so a provider whose own output default is larger keeps it.
    """
    window, source = resolve_context_window(
        provider_type=provider_type,
        context_window=context_window,
        served_context_window=served_context_window,
    )
    requested = requested_context_window(provider_type=provider_type, context_window=context_window)
    if window is None:
        return BudgetedContext(messages=messages, max_output_tokens=max_output_tokens)
    # The input limit is always computed against a bounded answer; sending that
    # bound is a separate question, answered per provider above.
    output_limit = max_output_tokens if max_output_tokens is not None else DEFAULT_OUTPUT_TOKENS
    sent_output_limit = (
        output_limit
        if max_output_tokens is not None or provider_type in SELF_ENFORCED_WINDOW_PROVIDERS
        else None
    )
    input_limit = window - output_limit - max(1024, window // 16)
    current = list(messages)
    costs = [_message_tokens(message) for message in messages]
    estimated = sum(costs) + _tool_tokens(tools, provider_type=provider_type)
    shortened: set[int] = set()
    # Oldest excerpts shrink first; recent results remain whole whenever they
    # fit. Never change any role, call ID, arguments, attachment, or system/user
    # text. Keeping every call and result makes parallel call groups intact too.
    for excerpt_bytes in (2048, 512, 128):
        for index, message in enumerate(messages):
            if estimated <= input_limit:
                return BudgetedContext(
                    messages=tuple(current),
                    max_output_tokens=sent_output_limit,
                    estimated_input_tokens=estimated,
                    input_token_limit=input_limit,
                    shortened_messages=len(shortened),
                    context_window=window,
                    context_window_source=source,
                    requested_context_window=requested,
                )
            if message.role not in {"tool", "assistant"}:
                continue
            if len(message.content.encode("utf-8")) <= excerpt_bytes:
                continue
            candidate = message.model_copy(
                update={"content": _shortened_content(message, excerpt_bytes=excerpt_bytes)}
            )
            cost = _message_tokens(candidate)
            if cost < costs[index]:
                estimated += cost - costs[index]
                current[index], costs[index] = candidate, cost
                shortened.add(index)
    if estimated > input_limit:
        # "Reduce the provided content" is not actionable without knowing by how
        # much and what dominates, and the cost is only knowable here: once this
        # raises, the assembled prompt is gone.
        by_role: dict[str, int] = {}
        for message, cost in zip(messages, costs, strict=True):
            by_role[message.role] = by_role.get(message.role, 0) + cost
        breakdown = ", ".join(
            f"{role} {by_role[role]}" for role in sorted(by_role, key=lambda r: -by_role[r])
        )
        per_tool = sorted(
            (
                (_tool_tokens((tool,), provider_type=provider_type) - 128, tool.name)
                for tool in tools
            ),
            reverse=True,
        )
        dearest = ", ".join(f"{name} {cost}" for cost, name in per_tool[:6])
        raise ModelProviderError(
            "The model context budget cannot fit the protected instructions, attachments, "
            "tool schemas and call arguments while reserving output space. Estimated "
            f"{estimated} input tokens against a {input_limit} limit ({window}-token window, "
            f"{source}, reserving {output_limit} for the answer). "
            f"{len(tools)} tool schemas cost {_tool_tokens(tools, provider_type=provider_type)}; "
            f"messages cost {breakdown} (only tool and assistant text can be shortened, "
            f"{len(shortened)} were). Dearest tools: {dearest}. Reduce the provided content "
            "or offered tools, or configure a supported context window.",
            retryable=False,
            error_code="model_context_budget_exceeded",
        )
    return BudgetedContext(
        messages=tuple(current),
        max_output_tokens=sent_output_limit,
        estimated_input_tokens=estimated,
        input_token_limit=input_limit,
        shortened_messages=len(shortened),
        context_window=window,
        context_window_source=source,
        requested_context_window=requested,
    )
