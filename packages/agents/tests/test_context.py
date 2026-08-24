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
        organization_context="Company directory (routing context only)",
        manager_context="Team status rollup",
        memory_context="Relevant memory:\n- prefers short answers",
    )
    system = build_messages(make_snapshot(), task)[0].content
    assert system.index("Senior SWE") < system.index("Company directory")
    assert system.index("Company directory") < system.index("Team status rollup")
    assert system.index("Team status rollup") < system.index("Relevant memory:")
    assert "prefers short answers" in system
    # Absent blocks add nothing.
    bare = build_messages(make_snapshot(), TaskContext(title="Chat", description=""))[0].content
    assert "Relevant memory" not in bare and "Company directory" not in bare


def test_history_and_instructions_become_turns() -> None:
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
    assert messages[-1].content == "Additional instruction: focus on tests"


def test_snapshot_hash_is_stable_and_config_sensitive() -> None:
    snapshot = make_snapshot()
    assert snapshot.snapshot_hash() == snapshot.snapshot_hash()
    changed = make_snapshot(system_prompt="Different prompt.")
    # Rebuild with same ids to isolate the prompt change.
    changed = snapshot.model_copy(update={"system_prompt": "Different prompt."})
    assert changed.snapshot_hash() != snapshot.snapshot_hash()
