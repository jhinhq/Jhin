"""Prompt composition tests (plan 7.2)."""

from uuid import uuid4

from jhin_agents.context import ConversationTurn, TaskContext, build_messages
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits


def make_snapshot(**overrides: object) -> AgentExecutionSnapshot:
    defaults: dict[str, object] = {
        "agent_id": uuid4(),
        "workspace_id": uuid4(),
        "workspace_name": "Acme Rockets",
        "name": "Senior SWE",
        "role_title": "Senior Software Engineer",
        "system_prompt": "You write production-quality software.",
        "autonomy_level": "supervised",
        "team_id": uuid4(),
        "team_name": "Engineering",
        "manager_agent_id": uuid4(),
        "manager_name": "CTO",
        "model_profile": ModelProfileSnapshot(
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
        "temperature": 0.3,
        "max_output_tokens": 512,
        "run_limits": RunLimits(max_steps=5, max_run_minutes=10),
    }
    defaults.update(overrides)
    return AgentExecutionSnapshot.model_validate(defaults)


def test_prompt_layers_in_order() -> None:
    messages = build_messages(
        make_snapshot(), TaskContext(title="Fix the login bug", description="Users get a 500.")
    )
    system = messages[0]
    assert system.role == "system"
    # Layer order (plan 7.2): platform preamble before role, role before org context.
    assert system.content.startswith(
        "You are Senior SWE, Senior Software Engineer, an AI teammate "
        "in the Acme Rockets workspace on Jhin."
    )
    assert system.content.index("Senior SWE") < system.content.index("Engineering team")
    assert "Your manager is CTO." in system.content
    assert "at most 5 reasoning steps" in system.content

    task = messages[1]
    assert task.role == "user"
    assert "Fix the login bug" in task.content
    assert "Users get a 500." in task.content


def test_context_blocks_follow_the_role_prompt_in_order() -> None:
    task = TaskContext(
        title="Chat",
        description="",
        organization_context="Your colleagues. Your manager:\n- CTO",
        manager_context="Team status rollup",
        memory_context="Relevant memory:\n- prefers short answers",
    )
    system = build_messages(make_snapshot(), task)[0].content
    assert system.index("Senior SWE") < system.index("Your colleagues.")
    assert system.index("Your colleagues.") < system.index("Team status rollup")
    assert system.index("Team status rollup") < system.index("Relevant memory:")
    assert "prefers short answers" in system
    # Absent blocks add nothing.
    bare = build_messages(make_snapshot(), TaskContext(title="Chat", description=""))[0].content
    assert "Relevant memory" not in bare and "Your colleagues." not in bare


def test_work_task_brief_precedes_history() -> None:
    """Assigned work, a trigger or a delegation gets its brief first: it frames
    the whole run rather than being the latest thing somebody said."""
    task = TaskContext(
        title="Chat",
        description="",
        history=(
            ConversationTurn(role="user", text="hello"),
            ConversationTurn(role="agent", text="hi, how can I help?"),
        ),
        user_instructions=("focus on tests",),
    )
    messages = build_messages(make_snapshot(), task)
    roles = [m.role for m in messages]
    assert roles == ["system", "user", "user", "assistant", "user"]
    assert messages[1].content.startswith("Task: ")
    assert messages[-1].content == "Additional instruction: focus on tests"


def test_a_chat_turn_ends_on_the_newest_user_message() -> None:
    """A chat turn's description is not a brief -- it is what the person just
    said, and the seed turn already carries it in its chronological place.
    Composing it as a brief as well stated the question twice and put it
    *before* everything said earlier, so the newest user message the provider
    saw was the previous turn's question and agents answered that instead."""
    task = TaskContext(
        title="Whos in your team?",
        description="Whos in your team?",
        conversation_turn=True,
        history=(
            ConversationTurn(role="user", text="Hey what is your name?"),
            ConversationTurn(role="agent", text="I'm Atlas."),
            ConversationTurn(role="user", text="Whos in your team?"),
        ),
    )
    messages = build_messages(make_snapshot(), task)
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1].content == "Whos in your team?"
    # Stated once, and never as a brief.
    assert sum("Whos in your team?" in m.content for m in messages) == 1
    assert not any(m.content.startswith("Task: ") for m in messages)


def test_chat_turn_question_precedes_this_steps_tool_transcript() -> None:
    """On a later step the question is no longer last -- the tool transcript
    it caused follows it. What must hold is that it is the last *user* turn,
    and that no user message trails a tool result."""
    task = TaskContext(
        title="Check the retries",
        description="Check the retries",
        conversation_turn=True,
        history=(
            ConversationTurn(role="user", text="Hey what is your name?"),
            ConversationTurn(role="agent", text="I'm Atlas."),
            ConversationTurn(role="user", text="Check the retries"),
            ConversationTurn(
                role="agent",
                text="",
                kind="tool_call",
                tool_call_id="c1",
                tool_name="system.echo",
                arguments_json='{"text": "hi"}',
            ),
            ConversationTurn(
                role="agent",
                text="hi",
                kind="tool_result",
                tool_call_id="c1",
                tool_name="system.echo",
            ),
        ),
    )
    messages = build_messages(make_snapshot(), task)
    assert [m.role for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    last_user = max(i for i, m in enumerate(messages) if m.role == "user")
    assert messages[last_user].content == "Check the retries"
    assert last_user < len(messages) - 1


def test_a_live_instruction_is_composed_once() -> None:
    """The API commits the instruction row before signalling the workflow, so
    on the step that drains it the same words arrive from the history and from
    user_instructions. The person said it once; the model should see it once."""
    task = TaskContext(
        title="Chat",
        description="",
        history=(ConversationTurn(role="user", text="Additional instruction: focus on tests"),),
        user_instructions=("focus on tests",),
    )
    messages = build_messages(make_snapshot(), task)
    rendered = [
        m.content for m in messages if m.content == "Additional instruction: focus on tests"
    ]
    assert len(rendered) == 1


def test_snapshot_hash_is_stable_and_config_sensitive() -> None:
    snapshot = make_snapshot()
    assert snapshot.snapshot_hash() == snapshot.snapshot_hash()
    changed = make_snapshot(system_prompt="Different prompt.")
    # Rebuild with same ids to isolate the prompt change.
    changed = snapshot.model_copy(update={"system_prompt": "Different prompt."})
    assert changed.snapshot_hash() != snapshot.snapshot_hash()
