"""Situational awareness in the prompt: the clock and the interlocutor.

These cover the pure rendering half (jhin_agents.context). The DB-backed
half — who the counterpart actually is, and which timezone the workspace
runs on — lives in services/agent_worker/tests/test_situation_context.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from jhin_agents.context import (
    Interlocutor,
    TaskContext,
    build_messages,
    format_local_time,
    interlocutor_block,
    time_block,
)
from jhin_agents.platform_prompt import render_platform_preamble
from jhin_agents.snapshot import AgentExecutionSnapshot, ModelProfileSnapshot, RunLimits

ROLE_PROMPT = "You write production-quality software."


def make_snapshot(**overrides: object) -> AgentExecutionSnapshot:
    defaults: dict[str, object] = {
        "agent_id": uuid4(),
        "workspace_id": uuid4(),
        "workspace_name": "Varand Test",
        "name": "Bisby",
        "role_title": "Chief of Staff",
        "system_prompt": ROLE_PROMPT,
        "autonomy_level": "supervised",
        "team_id": None,
        "team_name": None,
        "manager_agent_id": None,
        "manager_name": None,
        "model_profile": ModelProfileSnapshot(
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
        "temperature": None,
        "max_output_tokens": None,
        "run_limits": RunLimits(max_steps=20, max_run_minutes=30),
    }
    defaults.update(overrides)
    return AgentExecutionSnapshot.model_validate(defaults)


# --- time ---------------------------------------------------------------


def test_time_is_unambiguous_and_names_its_zone() -> None:
    # 2026-08-24 21:14 in a fixed -07:00 offset (what America/Los_Angeles is
    # on that date); the renderer takes the moment already localized.
    local = datetime(2026, 8, 24, 21, 14, tzinfo=timezone(timedelta(hours=-7)))
    assert (
        format_local_time(local, "America/Los_Angeles")
        == "Monday, 24 August 2026, 21:14 (America/Los_Angeles)"
    )
    block = time_block(local, "America/Los_Angeles")
    assert block.startswith("Current time: Monday, 24 August 2026, 21:14 (America/Los_Angeles).")
    # The model is told how to use it, so "what's today?" needs no tool call.
    assert "never guess the date" in block


def test_time_names_the_weekday_and_month_without_locale_help() -> None:
    # A Sunday and a single-digit day: no zero padding, English names.
    local = datetime(2026, 1, 4, 9, 5, tzinfo=UTC)
    assert format_local_time(local, "UTC") == "Sunday, 4 January 2026, 09:05 (UTC)"


def test_time_survives_a_blank_zone_name() -> None:
    local = datetime(2026, 8, 24, 21, 14, tzinfo=UTC)
    assert format_local_time(local, "") == "Monday, 24 August 2026, 21:14"


# --- interlocutor -------------------------------------------------------


def test_human_interlocutor_names_the_person_and_their_role_in_plain_words() -> None:
    block = interlocutor_block(
        [Interlocutor(display_name="Varand", kind="human", role="workspace owner")]
    )
    assert block.startswith(
        "Who you are talking with: Varand (workspace owner), a person in this workspace."
    )
    assert "Use their name when you address them." in block
    # Public identity only — no raw enum values, no addresses.
    assert "owner\n" not in block and "@" not in block


def test_delegated_child_task_names_the_requesting_agent_not_a_person() -> None:
    block = interlocutor_block(
        [
            Interlocutor(
                display_name="Bisby",
                kind="agent",
                role="Chief of Staff",
                relation="who delegated this task to you and is waiting for your result",
            )
        ]
    )
    assert "Bisby (Chief of Staff), an AI teammate in this workspace" in block
    assert "who delegated this task to you" in block
    assert "a person in this workspace" not in block


def test_unknown_interlocutor_renders_nothing() -> None:
    assert interlocutor_block([]) == ""
    # A person with no display name is skipped rather than guessed at.
    assert interlocutor_block([Interlocutor(display_name="   ")]) == ""


def test_several_participants_are_listed_and_bounded() -> None:
    people = [Interlocutor(display_name=f"Person {n}", role="workspace member") for n in range(9)]
    block = interlocutor_block(people)
    assert block.startswith("Who you are talking with:\n- Person 0 (workspace member)")
    assert "Person 4" in block
    assert "Person 5" not in block  # MAX_INTERLOCUTORS_LISTED


def test_names_are_flattened_and_length_bounded() -> None:
    block = interlocutor_block(
        [Interlocutor(display_name="Line\nbreak   injection", role="x" * 400)]
    )
    assert "Line break injection" in block
    # One line for the description, one for the guidance sentence.
    assert len(block.splitlines()) == 2
    assert "x" * 200 not in block


# --- composition --------------------------------------------------------


def _task(**overrides: object) -> TaskContext:
    defaults: dict[str, object] = {"title": "Chat", "description": ""}
    defaults.update(overrides)
    return TaskContext.model_validate(defaults)


def test_situation_blocks_sit_after_the_role_prompt_and_before_the_roster() -> None:
    local = datetime(2026, 8, 24, 21, 14, tzinfo=UTC)
    system = build_messages(
        make_snapshot(),
        _task(
            time_context=time_block(local, "UTC"),
            interlocutor_context=interlocutor_block(
                [Interlocutor(display_name="Varand", role="workspace owner")]
            ),
            organization_context="Company directory (routing context only)",
            memory_context="Recalled memory (curated records from earlier work)",
        ),
    )[0].content
    preamble = render_platform_preamble(
        agent_name="Bisby", role_title="Chief of Staff", workspace_name="Varand Test"
    )
    assert system.startswith(preamble)
    assert system.index(ROLE_PROMPT) < system.index("Current time:")
    assert system.index("Current time:") < system.index("Who you are talking with:")
    assert system.index("Who you are talking with:") < system.index("Company directory")
    assert system.index("Company directory") < system.index("Recalled memory")


def test_absent_situation_blocks_add_nothing() -> None:
    system = build_messages(make_snapshot(), _task())[0].content
    assert "Current time:" not in system
    assert "Who you are talking with" not in system


def test_composition_is_replay_stable_for_a_fixed_context() -> None:
    """The recorded-step contract: the same TaskContext always composes to
    the same bytes. Wall-clock reads happen in the activity that *builds*
    the context, never during composition, so re-running composition over a
    recorded context can never drift."""
    local = datetime(2026, 8, 24, 21, 14, tzinfo=UTC)
    snapshot = make_snapshot()
    task = _task(
        time_context=time_block(local, "America/Los_Angeles"),
        interlocutor_context=interlocutor_block([Interlocutor(display_name="Varand")]),
    )
    first = build_messages(snapshot, task)[0].content
    second = build_messages(snapshot, task)[0].content
    assert first == second
    # And a later clock is a different context, not a different composer.
    later = _task(
        time_context=time_block(local + timedelta(hours=1), "America/Los_Angeles"),
        interlocutor_context=task.interlocutor_context,
    )
    assert build_messages(snapshot, later)[0].content != first
