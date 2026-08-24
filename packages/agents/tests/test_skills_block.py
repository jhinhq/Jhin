"""The skills prompt block: formatting, bounds, and its place in the
system prompt (docs/architecture/skills.md)."""

from __future__ import annotations

from uuid import uuid4

from jhin_agents.context import (
    MAX_SKILLS_LISTED,
    TaskContext,
    build_messages,
    skills_block,
)
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits


def make_snapshot() -> AgentExecutionSnapshot:
    return AgentExecutionSnapshot.model_validate(
        {
            "agent_id": uuid4(),
            "workspace_id": uuid4(),
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
    )


class TestSkillsBlock:
    def test_empty_input_adds_nothing(self) -> None:
        assert skills_block(()) == ""
        assert skills_block([]) == ""

    def test_lists_names_with_descriptions_and_the_read_instruction(self) -> None:
        block = skills_block(
            [
                ("release-notes", "Write user-facing release notes."),
                ("code-review-checklist", "Review code methodically."),
            ]
        )
        assert block.startswith("Skills available to you")
        assert "- release-notes — Write user-facing release notes." in block
        assert "- code-review-checklist — Review code methodically." in block
        assert "skills.read" in block

    def test_bounds_count_and_description_length(self) -> None:
        many = [(f"skill-{index}", "d " * 400) for index in range(MAX_SKILLS_LISTED + 10)]
        block = skills_block(many)
        assert block.count("\n- ") == MAX_SKILLS_LISTED
        # Whitespace-collapsed and truncated with an ellipsis.
        first_line = block.splitlines()[1]
        assert len(first_line) < 350
        assert first_line.endswith("…")


class TestSkillsContextInPrompt:
    def test_skills_context_lands_after_memory_context(self) -> None:
        task = TaskContext(
            title="Chat",
            description="",
            memory_context="Relevant memory:\n- prefers short answers",
            skills_context=skills_block([("release-notes", "Write release notes.")]),
        )
        system = build_messages(make_snapshot(), task)[0].content
        assert "Skills available to you" in system
        assert system.index("Relevant memory:") < system.index("Skills available to you")

    def test_absent_skills_context_adds_nothing(self) -> None:
        system = build_messages(make_snapshot(), TaskContext(title="Chat", description=""))[
            0
        ].content
        assert "Skills available to you" not in system
