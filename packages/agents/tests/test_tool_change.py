"""The "your tools changed" notice: composed from two tool lists, verbatim."""

from uuid import uuid4

from jhin_agents.context import TaskContext, build_messages
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits
from jhin_agents.tool_change import MAX_LISTED_TOOLS, tools_changed_block


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
            base_url="http://model.test/v1",
            secret_id=None,
            model_name="test-mini",
            display_name="Test Mini",
            input_cost_micros_per_million=None,
            output_cost_micros_per_million=None,
        ),
        temperature=None,
        max_output_tokens=None,
        run_limits=RunLimits(max_steps=5, max_run_minutes=10),
    )


def test_added_and_removed_are_listed_sorted() -> None:
    block = tools_changed_block(
        ["memory.recall"],
        ["github.repository.read", "memory.recall", "github.branch.list", "github.file.read"],
    )
    assert block == (
        "Your tools changed since your last reply in this conversation. Added: "
        "github.branch.list, github.file.read, github.repository.read. Removed: none. Do not "
        "rely on anything you said about your tools before this turn."
    )


def test_removed_only_says_added_none() -> None:
    block = tools_changed_block(["linear.issue.read", "memory.recall"], ["memory.recall"])
    assert "Added: none. Removed: linear.issue.read." in block


def test_equal_sets_render_nothing() -> None:
    assert tools_changed_block(["a", "b"], ["b", "a"]) == ""
    assert tools_changed_block([], []) == ""


def test_long_lists_are_summarised_as_a_count() -> None:
    many = [f"tool.{index:02d}" for index in range(MAX_LISTED_TOOLS + 5)]
    block = tools_changed_block([], many)
    assert f"Added: {MAX_LISTED_TOOLS + 5} tools." in block
    exactly = [f"tool.{index:02d}" for index in range(MAX_LISTED_TOOLS)]
    assert "tool.00, tool.01" in tools_changed_block([], exactly)


def test_the_block_is_composed_into_the_system_prompt_after_the_situation() -> None:
    notice = tools_changed_block([], ["github.repository.read"])
    task = TaskContext(
        title="T",
        description="",
        time_context="Current time: now.",
        tools_changed_context=notice,
    )
    system = build_messages(make_snapshot(), task, has_tools=True)[0].content
    assert notice in system
    assert system.index("Current time: now.") < system.index(notice)
    assert system.index(notice) < system.index("You may call the provided tools")

    without = build_messages(make_snapshot(), TaskContext(title="T", description=""))[0].content
    assert "Your tools changed" not in without
