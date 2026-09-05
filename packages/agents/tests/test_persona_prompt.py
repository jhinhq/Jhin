"""The persona layer of the prompt: the "How you work" block, where it
sits, and how the run snapshot carries the card.

A persona shapes how an agent says things, never what it may do. Three
things make that true in the prompt and are pinned here: the guardrail line
is the first thing under the title, the block sits after the platform
preamble and before everything the agent or its organization wrote, and the
register facet rendered follows who is actually on the other side of the
conversation. The database half — the snapshot reading the card off the
agent row, and degrading to no card rather than a failed run — is here too,
against in-memory SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agents.context import (
    PERSONA_GUARDRAIL,
    Interlocutor,
    TaskContext,
    build_messages,
    interlocutor_block,
    interlocutor_kind,
    persona_block,
)
from jhin_agents.platform_prompt import PLATFORM_PREAMBLE_VERSION, render_platform_preamble
from jhin_agents.snapshot import (
    AgentExecutionSnapshot,
    ModelProfileSnapshot,
    RunLimits,
    resolve_snapshot,
)
from jhin_db.base import Base
from jhin_db.models import Agent, ModelProfile, ModelProvider, Persona, Workspace
from jhin_domain import new_uuid7
from jhin_personas import (
    MAX_FACET_CHARS,
    MAX_NEVER_ITEM_CHARS,
    MAX_NEVER_ITEMS,
    PersonaCard,
    PersonaFacets,
)

ROLE_PROMPT = "You write production-quality software."

# The exact sentence the contract fixes; pinned separately from the constant
# so a rewording of PERSONA_GUARDRAIL is a deliberate change here too.
GUARDRAIL_TEXT = (
    "This shapes how you say things, never what you may do: tool policy, approvals, "
    "safety rules, and your manager's instructions always win."
)

VOICE = "Dry, precise, quietly friendly. Sounds like the colleague who read the footnotes."
STANCE = "Separates what is known from what is assumed and says which is which."
PACE = "Short by default. Goes long only when a decision hinges on a detail."
WHEN_UNSURE = "Names the assumption, then asks the person one bounded question."
WITH_PEOPLE = "Warm and plain. Leads with the answer, follows with the caveat that matters."
WITH_TEAMMATES = "Terse and structured: claim, evidence, gap."
SIGNATURE = "Closes with one line starting 'Assumes:' when an answer rests on a guess."
NEVER = ["Hedge every sentence", "Bury the risk under the good news"]


def make_facets(**overrides: Any) -> PersonaFacets:
    values: dict[str, Any] = {
        "voice": VOICE,
        "stance": STANCE,
        "pace": PACE,
        "when_unsure": WHEN_UNSURE,
        "with_people": WITH_PEOPLE,
        "with_teammates": WITH_TEAMMATES,
        "signature": SIGNATURE,
        "never": list(NEVER),
    }
    values.update(overrides)
    return PersonaFacets(**values)


def make_card(**facets: Any) -> PersonaCard:
    return PersonaCard(
        name="the-skeptic",
        display_name="The Skeptic",
        description="Checks the claim before it becomes the plan.",
        tags=["professional", "review"],
        facets=make_facets(**facets),
    )


def make_snapshot(**overrides: object) -> AgentExecutionSnapshot:
    defaults: dict[str, object] = {
        "agent_id": uuid4(),
        "workspace_id": uuid4(),
        "workspace_name": "Varand Test",
        "name": "Bisby",
        "role_title": "Chief of Staff",
        "system_prompt": ROLE_PROMPT,
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
            input_cost_micros_per_million=None,
            output_cost_micros_per_million=None,
        ),
        "temperature": None,
        "max_output_tokens": None,
        "run_limits": RunLimits(max_steps=20, max_run_minutes=30),
    }
    defaults.update(overrides)
    return AgentExecutionSnapshot.model_validate(defaults)


# --- the block itself ---------------------------------------------------


def test_title_then_guardrail_verbatim_then_facets() -> None:
    lines = persona_block(make_card(), interlocutor_kind="human").split("\n")
    assert lines[0] == "How you work — The Skeptic"
    assert lines[1] == GUARDRAIL_TEXT
    assert PERSONA_GUARDRAIL == GUARDRAIL_TEXT
    # Every facet as a short labelled line, in prompt order, never joined
    # with "; ".
    assert lines[2:] == [
        f"- Voice: {VOICE}",
        f"- Stance: {STANCE}",
        f"- Pace: {PACE}",
        f"- When unsure: {WHEN_UNSURE}",
        f"- With people: {WITH_PEOPLE}",
        f"- Signature: {SIGNATURE}",
        "- Never: Hedge every sentence; Bury the risk under the good news",
    ]


@pytest.mark.parametrize(
    ("kind", "people", "teammates"),
    [("human", True, False), ("agent", False, True), (None, False, False)],
)
def test_the_register_facet_follows_who_is_on_the_other_side(
    kind: str | None, people: bool, teammates: bool
) -> None:
    """One card, three readings: "With people" for a person, "With
    teammates" for an agent, neither when nobody is there. Never both."""
    block = persona_block(make_card(), interlocutor_kind=kind)  # type: ignore[arg-type]
    assert (f"- With people: {WITH_PEOPLE}" in block) is people
    assert (f"- With teammates: {WITH_TEAMMATES}" in block) is teammates
    assert not ("- With people:" in block and "- With teammates:" in block)
    # The facets that do not depend on the counterpart are always there.
    for label in ("- Voice:", "- Stance:", "- Pace:", "- When unsure:", "- Signature:"):
        assert label in block


def test_empty_facets_are_omitted_rather_than_rendered_blank() -> None:
    block = persona_block(
        make_card(stance="", signature="", never=[]),
        interlocutor_kind="human",
    )
    assert "- Stance:" not in block
    assert "- Signature:" not in block
    assert "- Never:" not in block
    assert f"- Voice: {VOICE}" in block
    # A card with no register facet for this counterpart drops that line too.
    assert "- With people:" not in persona_block(
        make_card(with_people=""), interlocutor_kind="human"
    )


def test_caps_are_enforced_at_render_even_for_an_unvalidated_card() -> None:
    """The card is validated when written, but the prompt is the last line:
    a card that slipped past validation (recorded under a different rule
    set, say) still renders bounded, the way the other blocks bound their
    fields."""
    facets = PersonaFacets.model_construct(
        voice="v" * (MAX_FACET_CHARS + 50),
        stance="  spaced \n\n out  ",
        never=["n" * (MAX_NEVER_ITEM_CHARS + 20)] * (MAX_NEVER_ITEMS + 3),
    )
    card = PersonaCard.model_construct(
        name="oversized",
        display_name="D" * 200,
        description="d",
        tags=[],
        facets=facets,
    )
    lines = persona_block(card, interlocutor_kind="human").split("\n")
    title = lines[0].removeprefix("How you work — ")
    assert len(title) == 80 and title.endswith("…")
    voice = next(line for line in lines if line.startswith("- Voice: ")).removeprefix("- Voice: ")
    assert len(voice) == MAX_FACET_CHARS and voice.endswith("…")
    # Whitespace is collapsed, not trusted.
    assert "- Stance: spaced out" in lines
    never = next(line for line in lines if line.startswith("- Never: ")).removeprefix("- Never: ")
    items = never.split("; ")
    assert len(items) == MAX_NEVER_ITEMS
    assert all(len(item) == MAX_NEVER_ITEM_CHARS for item in items)


# --- interlocutor kind --------------------------------------------------


def test_interlocutor_kind_agrees_with_the_who_you_are_talking_with_block() -> None:
    person = Interlocutor(display_name="Varand", kind="human", role="workspace owner")
    agent = Interlocutor(display_name="Bisby", kind="agent", role="Chief of Staff")
    assert interlocutor_kind([person]) == "human"
    assert interlocutor_kind([agent]) == "agent"
    # The first one listed decides, which is who the block names first.
    assert interlocutor_kind([agent, person]) == "agent"
    # Nobody, or nobody with a name: no block, no kind.
    assert interlocutor_kind([]) is None
    nameless = Interlocutor(display_name="   ", kind="human")
    assert interlocutor_kind([nameless]) is None
    assert interlocutor_block([nameless]) == ""
    assert interlocutor_kind([nameless, person]) == "human"


# --- placement in the composed prompt -----------------------------------


def test_block_sits_after_the_preamble_and_before_everything_else() -> None:
    block = persona_block(make_card(), interlocutor_kind="human")
    snapshot = make_snapshot()
    task = TaskContext(
        title="Verify",
        description="",
        persona_context=block,
        time_context="Current time: Monday, 24 August 2026, 21:14 (UTC).",
        interlocutor_context="Who you are talking with: Varand (workspace owner).",
        organization_context="Your colleagues: CTO (manager).",
        manager_context="Team rollup: nothing in flight.",
        memory_context="What you remember: the release is on Friday.",
        skills_context="Skills available to you: release-notes.",
    )
    system = build_messages(snapshot, task, has_tools=True)[0].content
    preamble = render_platform_preamble(
        agent_name="Bisby", role_title="Chief of Staff", workspace_name="Varand Test"
    )
    assert system.startswith(preamble)
    # Directly after the preamble: nothing sits between them.
    assert system[len(preamble) :].startswith("\n\n" + block)
    order = [
        preamble,
        block,
        ROLE_PROMPT,
        "You are part of the Engineering team. Your manager is CTO.",
        task.time_context,
        task.interlocutor_context,
        "You may call the provided tools.",
        "Execution constraints:",
        task.organization_context,
        task.manager_context,
        task.memory_context,
        task.skills_context,
    ]
    positions = [system.index(piece) for piece in order]
    assert positions == sorted(positions), order


def test_no_persona_means_no_block() -> None:
    system = build_messages(make_snapshot(), TaskContext(title="Verify", description=""))[0].content
    assert "How you work" not in system
    assert PERSONA_GUARDRAIL not in system
    # The rest of the prompt is exactly as it was: the preamble still leads
    # straight into the role prompt.
    preamble = render_platform_preamble(
        agent_name="Bisby", role_title="Chief of Staff", workspace_name="Varand Test"
    )
    assert system[len(preamble) :].startswith("\n\n" + ROLE_PROMPT)


def test_preamble_version_is_untouched_by_the_persona_layer() -> None:
    """The block is a separate, snapshot-hashed layer, not a preamble
    wording change; the version audits key on stays where it was."""
    assert PLATFORM_PREAMBLE_VERSION == 12


# --- the snapshot carries the card --------------------------------------


def test_snapshot_carries_the_card_and_it_changes_the_hash() -> None:
    bare = make_snapshot()
    dressed = bare.model_copy(update={"persona": make_card()})
    assert dressed.persona == make_card()
    assert dressed.snapshot_hash() != bare.snapshot_hash()
    # Round trip through JSON, which is how the worker carries it between
    # activities.
    reloaded = AgentExecutionSnapshot.model_validate_json(dressed.model_dump_json())
    assert reloaded.persona == make_card()
    assert reloaded.snapshot_hash() == dressed.snapshot_hash()


def test_old_snapshot_json_without_a_persona_still_validates() -> None:
    payload = make_snapshot().model_dump(mode="json")
    assert payload.pop("persona", None) is None
    assert "persona" not in payload
    snapshot = AgentExecutionSnapshot.model_validate(payload)
    assert snapshot.persona is None


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


class World:
    workspace: Workspace
    agent: Agent


async def make_world(session: AsyncSession) -> World:
    world = World()
    world.workspace = Workspace(name="Test", slug=f"test-{new_uuid7().hex[:8]}")
    session.add(world.workspace)
    await session.flush()
    provider = ModelProvider(
        workspace_id=world.workspace.id, type="openai_compatible", display_name="Fake"
    )
    session.add(provider)
    await session.flush()
    profile = ModelProfile(
        workspace_id=world.workspace.id,
        provider_id=provider.id,
        model_name="fake-mini",
        display_name="Fake Mini",
    )
    session.add(profile)
    await session.flush()
    world.agent = Agent(
        workspace_id=world.workspace.id,
        name="Bisby",
        slug="bisby",
        model_profile_id=profile.id,
    )
    session.add(world.agent)
    await session.flush()
    return world


def persona_row(workspace: Workspace, card: PersonaCard, **overrides: Any) -> Persona:
    values: dict[str, Any] = {
        "workspace_id": workspace.id,
        "name": card.name,
        "display_name": card.display_name,
        "description": card.description,
        "tags_json": list(card.tags),
        "facets_json": card.facets.model_dump(),
        "source": "built_in",
        "enabled": True,
    }
    values.update(overrides)
    return Persona(**values)


async def test_resolve_snapshot_reads_the_card_off_the_agent_row(session: AsyncSession) -> None:
    world = await make_world(session)
    row = persona_row(world.workspace, make_card())
    session.add(row)
    await session.flush()
    bare = await resolve_snapshot(session, world.workspace.id, world.agent.id)
    assert bare.persona is None

    world.agent.persona_id = row.id
    await session.flush()
    dressed = await resolve_snapshot(session, world.workspace.id, world.agent.id)
    assert dressed.persona == make_card()
    # The hash on the run proves which card the run saw.
    assert dressed.snapshot_hash() != bare.snapshot_hash()


async def test_resolve_snapshot_skips_a_disabled_persona(session: AsyncSession) -> None:
    """Disabling keeps the assignment (re-enabling takes effect on the next
    run) but nothing of the card reaches this run."""
    world = await make_world(session)
    row = persona_row(world.workspace, make_card(), enabled=False)
    session.add(row)
    await session.flush()
    world.agent.persona_id = row.id
    await session.flush()
    snapshot = await resolve_snapshot(session, world.workspace.id, world.agent.id)
    assert snapshot.persona is None
    assert world.agent.persona_id == row.id


async def test_resolve_snapshot_degrades_a_malformed_card_to_no_persona(
    session: AsyncSession,
) -> None:
    """A stored document that no longer validates is not worth a failed run."""
    world = await make_world(session)
    row = persona_row(
        world.workspace,
        make_card(),
        facets_json={"voice": "x" * (MAX_FACET_CHARS + 1), "unknown": "facet"},
    )
    session.add(row)
    await session.flush()
    world.agent.persona_id = row.id
    await session.flush()
    snapshot = await resolve_snapshot(session, world.workspace.id, world.agent.id)
    assert snapshot.persona is None
    assert snapshot.name == "Bisby"


async def test_resolve_snapshot_ignores_a_persona_from_another_workspace(
    session: AsyncSession,
) -> None:
    world = await make_world(session)
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other)
    await session.flush()
    foreign = persona_row(other, make_card())
    session.add(foreign)
    await session.flush()
    world.agent.persona_id = foreign.id
    await session.flush()
    snapshot = await resolve_snapshot(session, world.workspace.id, world.agent.id)
    assert snapshot.persona is None
