"""Tool-calling prompt rebuild: transcript turns become provider messages
with the untrusted label on tool output (plan 7.3, 21.2)."""

from uuid import uuid4

from jhin_agents.context import UNTRUSTED_LABEL, ConversationTurn, TaskContext, build_messages
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits


def make_snapshot() -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot(
        agent_id=uuid4(),
        workspace_id=uuid4(),
        name="Scout",
        role_title="",
        system_prompt="",
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
            input_cost_micros_per_million=None,
            output_cost_micros_per_million=None,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=5, max_run_minutes=10),
    )


def test_tool_transcript_turns_map_to_provider_messages() -> None:
    task = TaskContext(
        title="Echo something",
        description="",
        history=(
            ConversationTurn(
                role="agent",
                text="",
                kind="tool_call",
                tool_call_id="call_0",
                tool_name="system.echo",
                arguments_json='{"text": "hi"}',
            ),
            ConversationTurn(
                role="agent",
                text='{"text": "hi"}',
                kind="tool_result",
                tool_call_id="call_0",
                tool_name="system.echo",
            ),
        ),
    )
    messages = build_messages(make_snapshot(), task, has_tools=True)

    call = messages[2]
    assert call.role == "assistant"
    assert len(call.tool_calls) == 1
    assert call.tool_calls[0].id == "call_0"
    assert call.tool_calls[0].name == "system.echo"
    assert call.tool_calls[0].arguments_json == '{"text": "hi"}'

    result = messages[3]
    assert result.role == "tool"
    assert result.tool_call_id == "call_0"
    assert result.content.startswith(UNTRUSTED_LABEL)
    assert '{"text": "hi"}' in result.content


def test_tool_guidance_only_when_tools_advertised() -> None:
    task = TaskContext(title="T", description="")
    with_tools = build_messages(make_snapshot(), task, has_tools=True)[0].content
    without_tools = build_messages(make_snapshot(), task, has_tools=False)[0].content
    assert "granted capabilities" in with_tools
    assert "granted capabilities" not in without_tools
    # A denial is relayed with the code and reason the result carries, not
    # paraphrased into "I'm blocked".
    assert "relay the error code and reason" in with_tools
    assert "do not retry" in with_tools
