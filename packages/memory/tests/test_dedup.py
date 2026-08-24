"""Semantic near-duplicate handling: the shared similarity rule, the policy's
skip/confirm vs supersede decision, self-reference and low-information
screening, and the persistence path that collapses paraphrase variants to a
single active record per scope."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, MemoryRecord, Team, Workspace
from jhin_domain import ActorType, MemoryScope, MemoryStatus, new_uuid7
from jhin_memory import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    compare_contents,
    content_hash,
    evaluate_candidate,
    is_low_information,
    is_self_referential,
)

WS = new_uuid7()
AGENT = new_uuid7()
TEAM = new_uuid7()

AGENT_ACTOR = ActorFacts(actor_type=ActorType.AGENT, actor_id=AGENT)


def source() -> SourceFacts:
    return SourceFacts(workspace_id=WS, agent_id=AGENT, visibility=MemoryScope.AGENT, team_id=TEAM)


def existing(
    content: str,
    *,
    subject: str | None = None,
    confidence: float = 0.5,
    embedding: tuple[float, ...] | None = None,
    embedding_model: str | None = None,
) -> ExistingRecord:
    return ExistingRecord(
        id=new_uuid7(),
        scope=MemoryScope.AGENT,
        scope_id=AGENT,
        status=MemoryStatus.ACTIVE,
        content_hash=content_hash(content),
        subject=subject,
        content=content,
        confidence=confidence,
        embedding=embedding,
        embedding_model=embedding_model,
    )


class TestSimilarityRule:
    def test_paraphrase_with_identical_informative_tokens_is_near_duplicate(self) -> None:
        verdict = compare_contents(
            "The release day is every other Thursday.",
            "Release day is every other Thursday.",
        )
        assert verdict.near_duplicate
        assert verdict.jaccard == 1.0

    def test_same_subject_wording_variant_is_near_duplicate(self) -> None:
        verdict = compare_contents(
            "We deploy every other Thursday.",
            "The release day is every other Thursday.",
            subject_a="deploy.day",
            subject_b="deploy.day",
        )
        assert verdict.near_duplicate

    def test_different_value_for_same_subject_is_not_near_duplicate(self) -> None:
        # Tuesday vs Thursday keeps the contradiction path available.
        verdict = compare_contents(
            "Deploys happen on Tuesday.",
            "Deploys happen on Thursday.",
            subject_a="deploy.day",
            subject_b="deploy.day",
        )
        assert not verdict.near_duplicate

    def test_unrelated_contents_are_not_near_duplicates(self) -> None:
        verdict = compare_contents(
            "We deploy every other Thursday.", "Varand prefers concise updates."
        )
        assert not verdict.near_duplicate

    def test_embedding_cosine_catches_semantic_duplicates(self) -> None:
        verdict = compare_contents(
            "Shipping cadence is biweekly.",
            "We release every second week.",
            embedding_a=(1.0, 0.0),
            embedding_b=(0.98, 0.15),
            embedding_model_a="m",
            embedding_model_b="m",
        )
        assert verdict.cosine is not None and verdict.cosine >= 0.9
        assert verdict.near_duplicate

    def test_embeddings_from_different_models_are_never_compared(self) -> None:
        verdict = compare_contents(
            "Shipping cadence is biweekly.",
            "We release every second week.",
            embedding_a=(1.0, 0.0),
            embedding_b=(1.0, 0.0),
            embedding_model_a="m1",
            embedding_model_b="m2",
        )
        assert verdict.cosine is None
        assert not verdict.near_duplicate


class TestPolicyNearDuplicates:
    def test_paraphrase_that_adds_nothing_is_skipped(self) -> None:
        record = existing("Release day is every other Thursday.", subject="deploy.day")
        decision = evaluate_candidate(
            MemoryCandidate(
                content="The release day is every other Thursday.", subject="deploy.day"
            ),
            source(),
            AGENT_ACTOR,
            [record],
        )
        assert decision.outcome == "duplicate"
        assert "near_duplicate" in decision.reasons
        assert decision.duplicate_of == record.id

    def test_same_subject_wording_variant_is_skipped_not_contested(self) -> None:
        record = existing("Release day is every other Thursday.", subject="deploy.day")
        decision = evaluate_candidate(
            MemoryCandidate(content="We deploy every other Thursday.", subject="deploy.day"),
            source(),
            AGENT_ACTOR,
            [record],
        )
        assert decision.outcome == "duplicate"
        assert "near_duplicate" in decision.reasons

    def test_meaningfully_better_duplicate_supersedes(self) -> None:
        record = existing(
            "Release day is every other Thursday.", subject="deploy.day", confidence=0.5
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="The release day is every other Thursday.",
                subject="deploy.day",
                confidence=0.9,
            ),
            source(),
            AGENT_ACTOR,
            [record],
        )
        assert decision.outcome == "activate"
        assert decision.supersedes == record.id
        assert "near_duplicate_superseded" in decision.reasons
        # The superseded record is a previous version, not a contradiction.
        assert decision.contested_with == ()
        assert decision.status is MemoryStatus.ACTIVE

    def test_semantic_duplicate_via_candidate_embedding(self) -> None:
        # No lexical overlap at all — only the embeddings reveal the match.
        record = existing(
            "The deployment cadence for releases is biweekly.",
            embedding=(1.0, 0.0),
            embedding_model="m",
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="We ship every second week."),
            source(),
            AGENT_ACTOR,
            [record],
            candidate_embedding=(0.99, 0.1),
            embedding_model="m",
        )
        assert decision.outcome == "duplicate"
        assert "near_duplicate" in decision.reasons

    def test_contradiction_still_fires_for_a_changed_value(self) -> None:
        record = existing("Deploys happen on Tuesday.", subject="deploy.day")
        decision = evaluate_candidate(
            MemoryCandidate(content="Deploys happen on Thursday.", subject="deploy.day"),
            source(),
            AGENT_ACTOR,
            [record],
        )
        assert decision.outcome == "activate"
        assert decision.status is MemoryStatus.CONTESTED
        assert decision.contested_with == (record.id,)


class TestSelfReferenceScreening:
    def test_agent_identity_facts_are_rejected(self) -> None:
        for content in (
            "The AI teammate's name is Bisby.",
            "Your name is Bisby.",
            "The assistant is named Bisby.",
            "Bisby is an AI teammate at the company.",
            "You are an AI teammate called Sparky.",
            "You are called Sparky.",
        ):
            decision = evaluate_candidate(
                MemoryCandidate(content=content), source(), AGENT_ACTOR, agent_name="Bisby"
            )
            assert decision.outcome == "reject", content
            assert "self_reference" in decision.reasons

    def test_facts_about_other_subjects_pass(self) -> None:
        for content in (
            "Varand prefers concise status updates.",
            "The customer's main contact is named Bob.",
            "Bisby should prioritise the deploy pipeline this week.",
        ):
            decision = evaluate_candidate(
                MemoryCandidate(content=content), source(), AGENT_ACTOR, agent_name="Bisby"
            )
            assert decision.outcome == "activate", content

    def test_is_self_referential_helper(self) -> None:
        assert is_self_referential("The AI teammate's name is Bisby.")
        assert is_self_referential("Bisby is a chatbot.", agent_name="Bisby")
        assert not is_self_referential("Bisby fixed the deploy pipeline.", agent_name="Bisby")

    def test_low_information_candidates_are_rejected(self) -> None:
        assert is_low_information("Hello!")
        assert is_low_information("ok")
        assert not is_low_information("Deploy Fridays")
        decision = evaluate_candidate(MemoryCandidate(content="Hi!"), source(), AGENT_ACTOR)
        assert decision.outcome == "reject"
        assert "low_information" in decision.reasons

    def test_explicit_human_remember_bypasses_quality_screens(self) -> None:
        human = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.AGENT,
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="Your name is Bisby."), source(), human
        )
        assert decision.outcome == "activate"


class World:
    workspace: Workspace
    team: Team
    agent: Agent


async def _world(session: AsyncSession) -> World:
    w = World()
    w.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
    session.add(w.workspace)
    await session.flush()
    w.team = Team(workspace_id=w.workspace.id, name="Eng")
    session.add(w.team)
    await session.flush()
    w.agent = Agent(workspace_id=w.workspace.id, name="Bisby", slug="bisby", team_id=w.team.id)
    session.add(w.agent)
    await session.flush()
    return w


class TestApplyCollapsesParaphrases:
    async def test_paraphrase_trio_collapses_to_one_active_record(
        self, session: AsyncSession
    ) -> None:
        w = await _world(session)
        src = SourceFacts(workspace_id=w.workspace.id, agent_id=w.agent.id, team_id=w.team.id)
        actor = ActorFacts(actor_type=ActorType.AGENT, actor_id=w.agent.id)

        for content in (
            "We deploy every other Thursday.",
            "The release day is every other Thursday.",
            "Release day is every other Thursday.",
        ):
            await apply_candidates(
                session,
                candidates=[MemoryCandidate(content=content, subject="deploy.day")],
                source=src,
                actor=actor,
                agent_name=w.agent.name,
            )
        await session.flush()

        rows = list(
            await session.scalars(
                select(MemoryRecord).where(MemoryRecord.workspace_id == w.workspace.id)
            )
        )
        active = [r for r in rows if r.status == MemoryStatus.ACTIVE.value]
        superseded = [r for r in rows if r.status == MemoryStatus.SUPERSEDED.value]
        assert len(active) == 1
        assert len(superseded) == len(rows) - 1
        # The second wording added information at equal confidence → it became
        # version 2; the third added nothing → confirmation bump only.
        assert active[0].version == 2
        assert active[0].supersedes_id is not None
        assert active[0].policy_json.get("confirmations") == 1
        assert active[0].policy_json.get("last_confirmed")

    async def test_candidate_embeddings_power_dedup_and_are_stored(
        self, session: AsyncSession
    ) -> None:
        w = await _world(session)
        src = SourceFacts(workspace_id=w.workspace.id, agent_id=w.agent.id, team_id=w.team.id)
        actor = ActorFacts(actor_type=ActorType.AGENT, actor_id=w.agent.id)

        first = await apply_candidates(
            session,
            candidates=[
                MemoryCandidate(content="The deployment cadence for releases is biweekly.")
            ],
            source=src,
            actor=actor,
            candidate_embeddings=[[1.0, 0.0]],
            embedding_model="m",
        )
        assert len(first.created) == 1
        assert first.created[0].embedding_json == [1.0, 0.0]
        assert first.created[0].embedding_model == "m"

        second = await apply_candidates(
            session,
            candidates=[MemoryCandidate(content="We ship every second week.")],
            source=src,
            actor=actor,
            candidate_embeddings=[[0.99, 0.1]],
            embedding_model="m",
        )
        assert second.created == []
        assert second.duplicates == 1
        assert second.confirmed == 1

    async def test_summary_counts_confirmed_and_superseded(self, session: AsyncSession) -> None:
        w = await _world(session)
        src = SourceFacts(workspace_id=w.workspace.id, agent_id=w.agent.id, team_id=w.team.id)
        actor = ActorFacts(actor_type=ActorType.AGENT, actor_id=w.agent.id)

        await apply_candidates(
            session,
            candidates=[MemoryCandidate(content="Release day is every other Thursday.")],
            source=src,
            actor=actor,
        )
        result = await apply_candidates(
            session,
            candidates=[
                MemoryCandidate(content="The release day is every other Thursday.", confidence=0.9)
            ],
            source=src,
            actor=actor,
        )
        summary = result.summary()
        assert summary["superseded"] == 1
        assert summary["confirmed"] == 0
        assert "near_duplicate_superseded" in summary["reasons"]
