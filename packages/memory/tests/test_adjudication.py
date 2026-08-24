"""Gray-zone LLM adjudication: the tri-state pair classification, strict
verdict parsing, the fail-safe adjudicator, resolver selection (workspace
default profile only), the fake-provider contract, and the write path merging
a paraphrase only the LLM tier can match — with caps respected."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, MemoryRecord, ModelProfile, ModelProvider, Team, Workspace
from jhin_domain import ActorType, MemoryScope, MemoryStatus, new_uuid7
from jhin_memory import (
    MAX_APPLY_ADJUDICATED_PAIRS,
    ActorFacts,
    AdjudicationPair,
    AdjudicationParseError,
    MemoryAdjudicator,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    build_adjudication_request,
    compare_contents,
    content_hash,
    parse_adjudication,
    resolve_memory_adjudicator,
)
from jhin_models import ModelClient, ModelProviderError, ModelRequest, ModelResponse, ModelUsage
from jhin_models.testing.fake_openai import build_completion

WS = new_uuid7()

LIVE_A = "We deploy every other Thursday."
LIVE_B = "The release day is every other Thursday."


class TestClassification:
    def test_rule_duplicate_is_classified_duplicate(self) -> None:
        verdict = compare_contents(
            "Release day is every other Thursday.",
            "The release day is every other Thursday.",
        )
        assert verdict.near_duplicate
        assert verdict.classification == "duplicate"

    def test_unrelated_pair_is_distinct(self) -> None:
        verdict = compare_contents(LIVE_A, "Varand prefers concise updates.")
        assert not verdict.near_duplicate
        assert verdict.classification == "distinct"

    def test_paraphrase_with_different_subjects_is_uncertain(self) -> None:
        # The live pair: token Jaccard 0.5 (< 0.6), different subjects, no
        # embeddings — the rule cannot merge it, only adjudication can.
        verdict = compare_contents(LIVE_A, LIVE_B, subject_a="deploy.days", subject_b="release.day")
        assert not verdict.near_duplicate
        assert verdict.classification == "uncertain"

    def test_same_subject_pair_is_never_distinct(self) -> None:
        verdict = compare_contents(
            "We deploy every other Thursday.",
            "We deploy every Friday.",
            subject_a="deploy.day",
            subject_b="deploy.day",
        )
        assert not verdict.near_duplicate
        assert verdict.classification == "uncertain"

    def test_mid_cosine_blocks_distinct(self) -> None:
        verdict = compare_contents(
            "Shipping cadence is biweekly.",
            "Varand prefers concise updates.",
            embedding_a=(1.0, 0.0),
            embedding_b=(0.8, 0.6),
            embedding_model_a="m",
            embedding_model_b="m",
        )
        assert verdict.cosine is not None and 0.70 <= verdict.cosine < 0.90
        assert verdict.classification == "uncertain"


class TestParsing:
    def test_valid_verdicts_are_case_insensitive(self) -> None:
        assert parse_adjudication('{"verdicts": ["SAME", "different"]}', 2) == [True, False]

    def test_fenced_json_is_accepted(self) -> None:
        assert parse_adjudication('```json\n{"verdicts": ["SAME"]}\n```', 1) == [True]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Sure! They look the same to me.",
            '{"verdicts": ["SAME"]}',  # wrong count (expected 2)
            '{"verdicts": ["SAME", "MAYBE"]}',
            '{"verdicts": ["SAME", 1]}',
            '{"verdicts": ["SAME", "DIFFERENT"], "notes": "x"}',
            '{"verdicts": "SAME"}',
            "[true, false]",
        ],
    )
    def test_garbage_is_rejected(self, text: str) -> None:
        with pytest.raises(AdjudicationParseError):
            parse_adjudication(text, 2)


class TestRequestShape:
    def test_one_compact_request_for_all_pairs(self) -> None:
        pairs = [
            AdjudicationPair(
                content_a=LIVE_A, content_b=LIVE_B, subject_a="deploy.days", subject_b=None
            ),
            AdjudicationPair(content_a="x" * 400, content_b="y"),
        ]
        request = build_adjudication_request(model="fake-mini", pairs=pairs)
        assert request.temperature == 0.0
        assert request.messages[0].role == "system"
        assert "SAME only when" in request.messages[0].content
        assert "DIFFERENT" in request.messages[0].content
        user = request.messages[1].content
        assert "Pair 1 (subjects: deploy.days | -)" in user
        assert "Pair 2" in user
        assert f"A: {LIVE_A}" in user
        # Long contents are truncated to keep the request bounded.
        assert "x" * 301 not in user


class StubClient(ModelClient):
    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(text=self.text, usage=ModelUsage(input_tokens=5, output_tokens=2))

    def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        raise NotImplementedError

    async def verify(self) -> str:
        return "ok"

    async def close(self) -> None:
        self.closed = True


class FakeProviderClient(StubClient):
    """Pipes requests through the fake provider's pure completion logic, so
    the adjudication contract is exercised end to end offline."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        body = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        status, payload = build_completion(body)
        assert status == 200
        return ModelResponse(text=str(payload["choices"][0]["message"]["content"]))


class TestAdjudicator:
    async def test_verdicts_are_returned_in_order(self) -> None:
        client = StubClient('{"verdicts": ["SAME", "DIFFERENT"]}')
        adjudicator = MemoryAdjudicator(client, model="fake-mini")
        pairs = [
            AdjudicationPair(content_a=LIVE_A, content_b=LIVE_B),
            AdjudicationPair(content_a="a", content_b="b"),
        ]
        assert await adjudicator.adjudicate(pairs, workspace_id=WS) == [True, False]
        assert len(client.requests) == 1  # one compact request for all pairs

    async def test_empty_pairs_never_call_the_model(self) -> None:
        client = StubClient('{"verdicts": []}')
        adjudicator = MemoryAdjudicator(client, model="fake-mini")
        assert await adjudicator.adjudicate([], workspace_id=WS) == []
        assert client.requests == []

    async def test_provider_error_means_all_different(self) -> None:
        client = StubClient(error=ModelProviderError("boom", status_code=500, retryable=True))
        adjudicator = MemoryAdjudicator(client, model="fake-mini")
        pairs = [AdjudicationPair(content_a=LIVE_A, content_b=LIVE_B)]
        assert await adjudicator.adjudicate(pairs, workspace_id=WS) == [False]

    async def test_unparseable_output_means_all_different(self) -> None:
        client = StubClient("[fake-mini] Completed: they look the same")
        adjudicator = MemoryAdjudicator(client, model="fake-mini")
        pairs = [AdjudicationPair(content_a=LIVE_A, content_b=LIVE_B)]
        assert await adjudicator.adjudicate(pairs, workspace_id=WS) == [False]

    async def test_fake_provider_contract_matches_the_live_pair(self) -> None:
        adjudicator = MemoryAdjudicator(FakeProviderClient(), model="fake-mini")
        pairs = [
            # Same rare value token (thursday) on both sides → SAME.
            AdjudicationPair(
                content_a=LIVE_A, content_b=LIVE_B, subject_a="deploy.days", subject_b="release.day"
            ),
            # Conflicting weekday value → DIFFERENT.
            AdjudicationPair(
                content_a="We deploy every other Thursday.", content_b="We deploy every Friday."
            ),
        ]
        assert await adjudicator.adjudicate(pairs, workspace_id=WS) == [True, False]


class TestResolver:
    async def _workspace(
        self, session: AsyncSession, *, default: bool = True, enabled: bool = True
    ) -> Workspace:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        provider = ModelProvider(
            workspace_id=workspace.id,
            type="openai_compatible",
            display_name="fake",
            base_url="http://fake",
            enabled=enabled,
        )
        session.add(provider)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_id=provider.id,
            display_name="fake-mini",
            model_name="fake-mini",
        )
        session.add(profile)
        await session.flush()
        if default:
            workspace.default_model_profile_id = profile.id
        await session.flush()
        return workspace

    async def test_workspace_default_profile_is_used(self, session: AsyncSession) -> None:
        workspace = await self._workspace(session)
        adjudicator = await resolve_memory_adjudicator(session, None, workspace_id=workspace.id)
        assert adjudicator is not None
        assert adjudicator.model == "fake-mini"
        await adjudicator.close()

    async def test_no_default_profile_skips_adjudication(self, session: AsyncSession) -> None:
        workspace = await self._workspace(session, default=False)
        assert (await resolve_memory_adjudicator(session, None, workspace_id=workspace.id)) is None

    async def test_disabled_provider_skips_adjudication(self, session: AsyncSession) -> None:
        workspace = await self._workspace(session, enabled=False)
        assert (await resolve_memory_adjudicator(session, None, workspace_id=workspace.id)) is None


class ScriptedAdjudicator:
    """Deterministic in-test adjudicator: SAME when both statements carry the
    word "thursday" (the live pair) and no conflicting weekday."""

    def __init__(self) -> None:
        self.calls: list[list[AdjudicationPair]] = []

    @staticmethod
    def _same(pair: AdjudicationPair) -> bool:
        a, b = pair.content_a.casefold(), pair.content_b.casefold()
        days = ("monday", "tuesday", "wednesday", "thursday", "friday")
        values_a = {d for d in days if d in a}
        values_b = {d for d in days if d in b}
        return bool(values_a) and values_a == values_b

    async def adjudicate(
        self, pairs: Sequence[AdjudicationPair], *, workspace_id: UUID
    ) -> list[bool]:
        self.calls.append(list(pairs))
        return [self._same(pair) for pair in pairs]


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


def _source(w: World) -> SourceFacts:
    return SourceFacts(workspace_id=w.workspace.id, agent_id=w.agent.id, team_id=w.team.id)


def _actor(w: World) -> ActorFacts:
    return ActorFacts(actor_type=ActorType.AGENT, actor_id=w.agent.id)


def _record(w: World, content: str, *, subject: str | None = None) -> MemoryRecord:
    return MemoryRecord(
        workspace_id=w.workspace.id,
        scope=MemoryScope.AGENT.value,
        scope_id=w.agent.id,
        kind="fact",
        subject=subject,
        content=content,
        content_hash=content_hash(content),
        visibility="agent",
        status=MemoryStatus.ACTIVE.value,
        created_by_type="agent",
        created_by_id=w.agent.id,
    )


class TestWritePathAdjudication:
    async def test_adjudicated_paraphrase_confirms_instead_of_duplicating(
        self, session: AsyncSession
    ) -> None:
        w = await _world(session)
        session.add(_record(w, LIVE_B, subject="release.day"))
        await session.flush()
        adjudicator = ScriptedAdjudicator()

        result = await apply_candidates(
            session,
            candidates=[MemoryCandidate(content=LIVE_A, subject="deploy.days")],
            source=_source(w),
            actor=_actor(w),
            agent_name=w.agent.name,
            adjudicator=adjudicator,
        )
        assert result.created == []
        assert result.duplicates == 1
        assert result.adjudicated == 1
        assert result.summary()["adjudicated"] == 1
        assert "adjudicated_same" in result.decisions[0].reasons
        assert len(adjudicator.calls) == 1 and len(adjudicator.calls[0]) == 1

        rows = list(
            await session.scalars(
                select(MemoryRecord).where(MemoryRecord.workspace_id == w.workspace.id)
            )
        )
        active = [r for r in rows if r.status == MemoryStatus.ACTIVE.value]
        assert len(active) == 1
        assert active[0].content == LIVE_B
        assert active[0].policy_json.get("confirmations") == 1

    async def test_value_change_stays_distinct_and_contested(self, session: AsyncSession) -> None:
        w = await _world(session)
        session.add(_record(w, "We deploy every other Thursday.", subject="deploy.day"))
        await session.flush()
        adjudicator = ScriptedAdjudicator()

        result = await apply_candidates(
            session,
            candidates=[MemoryCandidate(content="We deploy every Friday.", subject="deploy.day")],
            source=_source(w),
            actor=_actor(w),
            agent_name=w.agent.name,
            adjudicator=adjudicator,
        )
        assert len(result.created) == 1
        assert result.adjudicated == 1
        assert result.created[0].status == MemoryStatus.CONTESTED.value
        rows = list(
            await session.scalars(
                select(MemoryRecord).where(MemoryRecord.workspace_id == w.workspace.id)
            )
        )
        assert all(r.status != MemoryStatus.SUPERSEDED.value for r in rows)
        assert len(rows) == 2

    async def test_apply_cap_limits_pairs_per_call(self, session: AsyncSession) -> None:
        w = await _world(session)
        letters = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")
        for letter in letters:
            session.add(_record(w, f"Deploy {letter} note.", subject="deploy.day"))
        await session.flush()
        adjudicator = ScriptedAdjudicator()

        result = await apply_candidates(
            session,
            candidates=[
                MemoryCandidate(content="We deploy every other Thursday.", subject="deploy.day")
            ],
            source=_source(w),
            actor=_actor(w),
            agent_name=w.agent.name,
            adjudicator=adjudicator,
        )
        assert result.adjudicated == MAX_APPLY_ADJUDICATED_PAIRS
        assert len(adjudicator.calls) == 1
        assert len(adjudicator.calls[0]) == MAX_APPLY_ADJUDICATED_PAIRS

    async def test_without_adjudicator_the_gray_zone_stays_split(
        self, session: AsyncSession
    ) -> None:
        w = await _world(session)
        session.add(_record(w, LIVE_B, subject="release.day"))
        await session.flush()
        result = await apply_candidates(
            session,
            candidates=[MemoryCandidate(content=LIVE_A, subject="deploy.days")],
            source=_source(w),
            actor=_actor(w),
            agent_name=w.agent.name,
        )
        assert len(result.created) == 1
        assert result.adjudicated == 0
