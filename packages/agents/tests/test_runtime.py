"""Runtime step execution and cost estimation tests."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from jhin_agents.context import UNTRUSTED_LABEL, ConversationTurn, TaskContext, build_messages
from jhin_agents.runtime import estimate_cost_micros, execute_step
from jhin_agents.snapshot import (
    AgentExecutionSnapshot,
    ModelProfileSnapshot,
    RunLimits,
    _reasoning_override,
)
from jhin_models import (
    ModelClient,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ReasoningConfig,
    ToolSchema,
    WebSearchConfig,
)


def make_snapshot() -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot(
        agent_id=uuid4(),
        workspace_id=uuid4(),
        name="Senior SWE",
        role_title="Senior Software Engineer",
        system_prompt="You write production-quality software.",
        autonomy_level="supervised",
        team_id=None,
        team_name=None,
        manager_agent_id=None,
        manager_name=None,
        model_profile=ModelProfileSnapshot(
            profile_id=uuid4(),
            provider_id=uuid4(),
            provider_type="openai_compatible",
            base_url="http://fake:8080/v1",
            secret_id=None,
            model_name="fake-mini",
            display_name="Fake Mini",
            input_cost_micros_per_million=1_000_000,
            output_cost_micros_per_million=2_000_000,
        ),
        temperature=0.3,
        max_output_tokens=512,
        run_limits=RunLimits(max_steps=5, max_run_minutes=10),
    )


class FakeClient(ModelClient):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text="Done: implemented.",
            finish_reason="stop",
            model=request.model,
            usage=ModelUsage(input_tokens=100, output_tokens=20, cached_tokens=0),
            latency_ms=12,
            provider_request_id="req-1",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        yield "unused"

    async def verify(self) -> str:
        return "ok"

    async def close(self) -> None:
        pass


async def test_execute_step_runs_load_context_then_reason() -> None:
    client = FakeClient()
    snapshot = make_snapshot()
    outcome = await execute_step(client, snapshot, TaskContext(title="Do it", description=""))

    assert outcome.done is True
    assert outcome.text == "Done: implemented."
    assert outcome.usage.input_tokens == 100
    assert [t.node for t in outcome.transitions] == ["load_context", "reason"]
    # The model call used the snapshot's profile and limits.
    request = client.requests[0]
    assert request.model == snapshot.model_profile.model_name
    assert request.temperature == snapshot.temperature
    assert request.max_output_tokens == snapshot.max_output_tokens
    # No profile opt-in means no model-native web search, and no reasoning
    # opinion (the adapter applies the automatic tool-compatibility rule).
    assert request.web_search is None
    assert request.reasoning is None


async def test_execute_step_nudge_appends_a_final_user_message() -> None:
    client = FakeClient()
    snapshot = make_snapshot()
    await execute_step(
        client,
        snapshot,
        TaskContext(title="Do it", description=""),
        nudge="You returned no reply. Answer the person now.",
    )
    messages = client.requests[0].messages
    assert messages[-1].role == "user"
    assert messages[-1].content == "You returned no reply. Answer the person now."


async def test_execute_step_passes_profile_web_search_to_the_adapter() -> None:
    client = FakeClient()
    base = make_snapshot()
    profile = base.model_profile.model_copy(
        update={"web_search": WebSearchConfig(enabled=True, max_uses=3)}
    )
    snapshot = base.model_copy(update={"model_profile": profile})
    await execute_step(client, snapshot, TaskContext(title="Fresh info", description=""))

    request = client.requests[0]
    assert request.web_search is not None
    assert request.web_search.enabled is True
    assert request.web_search.max_uses == 3


async def test_execute_step_passes_profile_reasoning_to_the_adapter() -> None:
    client = FakeClient()
    base = make_snapshot()
    profile = base.model_profile.model_copy(
        update={"reasoning": ReasoningConfig(effort="none", supports_reasoning=True)}
    )
    snapshot = base.model_copy(update={"model_profile": profile})
    await execute_step(client, snapshot, TaskContext(title="Think", description=""))

    request = client.requests[0]
    assert request.reasoning is not None
    assert request.reasoning.effort == "none"
    assert request.reasoning.supports_reasoning is True


@pytest.mark.parametrize(
    "body", ["archive result " * 12_000, "研究资料😀" * 12_000], ids=["ascii", "unicode"]
)
async def test_ollama_context_reserves_output_and_preserves_authority_and_tool_pairs(body):
    client = FakeClient()
    base = make_snapshot()
    snapshot = base.model_copy(
        update={
            "max_output_tokens": None,
            "model_profile": base.model_profile.model_copy(update={"provider_type": "ollama"}),
        }
    )
    task = TaskContext(
        title="Draft the researched article",
        description="Only Ashley may publish; keep this assignment draft_only.",
        history=(
            ConversationTurn(role="user", text="Use the creator workspace photo search."),
            ConversationTurn(
                role="agent",
                text="Reading the archive",
                kind="tool_call",
                tool_call_id="persisted-call-123",
                tool_name="ghost.posts.list",
                arguments_json='{"page":1,"limit":20}',
            ),
            ConversationTurn(
                role="agent",
                text=body,
                kind="tool_result",
                tool_call_id="persisted-call-123",
                tool_name="ghost.posts.list",
            ),
        ),
        user_instructions=("Do not publish or disclose credentials.",),
    )
    tools = (ToolSchema(name="ghost.posts.list", parameters={"type": "object"}),)
    original = build_messages(snapshot, task, has_tools=True)

    await execute_step(client, snapshot, task, tools)

    request = client.requests[0]
    assert request.max_output_tokens == 4096
    assert request.messages[0] == original[0]
    assert [m for m in request.messages if m.role == "user"] == [
        m for m in original if m.role == "user"
    ]
    assert request.messages[3].tool_calls == original[3].tool_calls
    result = request.messages[4]
    assert result.tool_call_id == "persisted-call-123"
    assert result.content.startswith(UNTRUSTED_LABEL)
    assert "persisted-call-123" in result.content
    assert "incomplete" in result.content
    assert len(result.content.encode("utf-8")) < 4096
    assert body not in result.content
    assert request.tools == tools


async def test_context_budget_rejects_oversized_protected_instructions_before_dispatch():
    client = FakeClient()
    base = make_snapshot()
    snapshot = base.model_copy(
        update={"model_profile": base.model_profile.model_copy(update={"context_window": 16_384})}
    )
    with pytest.raises(ModelProviderError, match="context budget") as raised:
        await execute_step(
            client, snapshot, TaskContext(title="Work", description="human instruction " * 10_000)
        )
    assert raised.value.error_code == "model_context_budget_exceeded"
    assert not client.requests


async def test_context_budget_includes_expanded_ollama_tool_schemas():
    client = FakeClient()
    base = make_snapshot()
    snapshot = base.model_copy(
        update={
            "model_profile": base.model_profile.model_copy(
                update={"provider_type": "ollama", "context_window": 16_384}
            )
        }
    )
    tool = ToolSchema(
        name="large.schema",
        parameters={
            "$defs": {"Body": {"type": "string", "description": "schema text " * 1000}},
            "type": "object",
            "properties": {f"field_{i}": {"$ref": "#/$defs/Body"} for i in range(4)},
        },
    )
    with pytest.raises(ModelProviderError, match="context budget"):
        await execute_step(client, snapshot, TaskContext(title="Work", description=""), (tool,))
    assert not client.requests


def test_reasoning_override_folds_in_the_supports_reasoning_column() -> None:
    assert _reasoning_override(None, False) is None
    assert _reasoning_override({}, False) is None
    assert _reasoning_override({"reasoning": {"effort": "bogus"}}, False) is None

    flagged = _reasoning_override({}, True)
    assert flagged is not None and flagged.supports_reasoning is True
    assert flagged.effort is None

    pinned = _reasoning_override({"reasoning": {"effort": "high"}}, False)
    assert pinned is not None and pinned.effort == "high"
    assert pinned.supports_reasoning is False


def test_estimate_cost_micros() -> None:
    usage = ModelUsage(input_tokens=1_000_000, output_tokens=500_000)
    # $1/M input (1_000_000 micros), $2/M output.
    cost = estimate_cost_micros(usage, 1_000_000, 2_000_000)
    assert cost == 1_000_000 + 1_000_000

    assert estimate_cost_micros(usage, None, None) == 0
    # Integer floor, no floats anywhere.
    assert estimate_cost_micros(ModelUsage(input_tokens=1, output_tokens=0), 1_000_000, None) == 1
