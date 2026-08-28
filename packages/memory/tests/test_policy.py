"""Deterministic memory policy: screening, non-amplification, dedup,
contradiction, and promotion rules (no I/O)."""

from __future__ import annotations

import pytest

from jhin_domain import ActorType, MemoryScope, MemorySensitivity, MemoryStatus, new_uuid7
from jhin_memory import (
    ActorFacts,
    ExistingRecord,
    MemoryCandidate,
    SourceFacts,
    content_hash,
    evaluate_candidate,
    normalize_content,
    screen_content,
)

WS = new_uuid7()
AGENT = new_uuid7()
TEAM = new_uuid7()


def source(visibility: MemoryScope = MemoryScope.AGENT, **overrides: object) -> SourceFacts:
    values: dict[str, object] = {
        "workspace_id": WS,
        "agent_id": AGENT,
        "visibility": visibility,
        "team_id": TEAM,
    }
    values.update(overrides)
    return SourceFacts.model_validate(values)


AGENT_ACTOR = ActorFacts(actor_type=ActorType.AGENT, actor_id=AGENT)


class TestScreening:
    @pytest.mark.parametrize(
        "text",
        [
            "The API key is sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".abc",
            "use bearer abcdefghij1234567890abcdefghij when calling the API",
            "DATABASE_URL=postgresql://jhin:hunter2@db:5432/jhin",
            "Their GitHub token is ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "-----BEGIN RSA PRIVATE KEY----- MIIE...",
            "aws key AKIAIOSFODNN7EXAMPLE is used for s3",
            "api_key = 'abcdef12345678'",
        ],
    )
    def test_credentials_are_rejected(self, text: str) -> None:
        result = screen_content(text)
        assert result.rejected
        assert result.content == ""
        assert any(reason.startswith("secret:") for reason in result.reasons)

    def test_password_assignment_is_redacted(self) -> None:
        result = screen_content("The staging password is hunter2 and rotates monthly.")
        assert not result.rejected
        assert result.redacted
        assert "hunter2" not in result.content
        assert "[REDACTED]" in result.content

    def test_ordinary_text_passes(self) -> None:
        result = screen_content("Varand prefers concise status updates on Mondays.")
        assert not result.rejected
        assert not result.redacted
        assert result.reasons == ()

    def test_policy_rejects_secret_candidate(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Deploy key: sk-proj-abcdefghijklmnopqrstuvwxyz123456"),
            source(),
            AGENT_ACTOR,
        )
        assert decision.outcome == "reject"
        assert any(r.startswith("secret:") for r in decision.reasons)

    def test_policy_stores_redacted_with_sensitivity(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="The wifi password is hunter2."), source(), AGENT_ACTOR
        )
        assert decision.outcome == "activate"
        assert decision.sensitivity is MemorySensitivity.REDACTED
        assert "hunter2" not in decision.content


class TestNonAmplification:
    def test_agent_source_cannot_become_team_memory(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="We ship on Tuesdays.", requested_scope=MemoryScope.TEAM),
            source(MemoryScope.AGENT),
            AGENT_ACTOR,
        )
        assert decision.outcome == "reject"
        assert "non_amplification" in decision.reasons

    def test_team_source_cannot_become_workspace_memory(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="We ship on Tuesdays.", requested_scope=MemoryScope.WORKSPACE),
            source(MemoryScope.TEAM),
            AGENT_ACTOR,
        )
        assert decision.outcome == "reject"
        assert "non_amplification" in decision.reasons

    def test_internal_source_is_never_memory(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Something from hidden reasoning."),
            source(internal=True),
            AGENT_ACTOR,
        )
        assert decision.outcome == "reject"
        assert "source_internal" in decision.reasons

    def test_visibility_never_exceeds_source(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="We ship on Tuesdays.", requested_scope=MemoryScope.AGENT),
            source(MemoryScope.TEAM),
            AGENT_ACTOR,
        )
        assert decision.visibility is MemoryScope.TEAM
        assert decision.scope is MemoryScope.AGENT

    def test_explicit_human_remember_needs_authority(self) -> None:
        member = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.AGENT,
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="Company holiday is Friday.", requested_scope=MemoryScope.WORKSPACE
            ),
            source(MemoryScope.AGENT),
            member,
        )
        assert decision.outcome == "reject"
        assert "insufficient_authority" in decision.reasons

    def test_explicit_admin_remember_activates_workspace_memory(self) -> None:
        admin = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.WORKSPACE,
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="Company holiday is Friday.", requested_scope=MemoryScope.WORKSPACE
            ),
            source(MemoryScope.AGENT),
            admin,
        )
        assert decision.outcome == "activate"
        assert decision.status is MemoryStatus.ACTIVE
        assert decision.scope_id == WS

    def test_a_person_may_vouch_for_words_they_wrote(self) -> None:
        """The explicit path skips the quality screens because the human is
        the authority on their own statement. ``authored_by_model=False`` is
        the default, so every existing caller keeps that."""
        admin = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.WORKSPACE,
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="ok", requested_scope=MemoryScope.WORKSPACE),
            source(MemoryScope.AGENT),
            admin,
        )
        assert decision.outcome == "activate"

    def test_a_scope_a_person_authorised_does_not_vouch_for_the_wording(self) -> None:
        """An agent citing an answered question widens the *scope* on the
        person's authority. Nobody looked at the words, so the quality
        screens stay on — otherwise "ok" becomes company-wide memory."""
        authorised = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.WORKSPACE,
            authored_by_model=True,
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="ok", requested_scope=MemoryScope.WORKSPACE),
            source(MemoryScope.AGENT),
            authorised,
        )
        assert decision.outcome == "reject"
        assert "low_information" in decision.reasons

    def test_a_model_authored_memory_still_bypasses_non_amplification(self) -> None:
        """The widening is the whole point: a chat is agent-visible, and the
        person's answer is what lets a real fact out of it."""
        authorised = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.WORKSPACE,
            authored_by_model=True,
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="Engineering deploys on Mondays at 9am PST.",
                requested_scope=MemoryScope.TEAM,
            ),
            source(MemoryScope.AGENT),
            authorised,
        )
        assert decision.outcome == "activate"
        assert decision.status is MemoryStatus.ACTIVE
        assert decision.scope is MemoryScope.TEAM
        assert decision.scope_id == TEAM
        assert "non_amplification" not in decision.reasons

    def test_a_model_authored_memory_still_needs_the_authority(self) -> None:
        authorised = ActorFacts(
            actor_type=ActorType.USER,
            actor_id=new_uuid7(),
            explicit=True,
            authority=MemoryScope.AGENT,
            authored_by_model=True,
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="The company holiday is the first Friday of July.",
                requested_scope=MemoryScope.WORKSPACE,
            ),
            source(MemoryScope.AGENT),
            authorised,
        )
        assert decision.outcome == "reject"
        assert "insufficient_authority" in decision.reasons

    def test_agent_actor_cannot_claim_explicit(self) -> None:
        """``explicit`` only means something for authenticated humans."""
        fake = ActorFacts(
            actor_type=ActorType.AGENT,
            actor_id=AGENT,
            explicit=True,
            authority=MemoryScope.WORKSPACE,
        )
        decision = evaluate_candidate(
            MemoryCandidate(
                content="Company holiday is Friday.", requested_scope=MemoryScope.WORKSPACE
            ),
            source(MemoryScope.AGENT),
            fake,
        )
        assert decision.outcome == "reject"
        assert "non_amplification" in decision.reasons


class TestNormalizationAndDedup:
    def test_normalization_is_case_space_punct_insensitive(self) -> None:
        assert normalize_content("  Ship on   Tuesdays! ") == normalize_content("ship on tuesdays")
        assert content_hash("Ship on Tuesdays.") == content_hash("ship on tuesdays")

    def test_exact_duplicate_in_scope_is_not_stored(self) -> None:
        existing = ExistingRecord(
            id=new_uuid7(),
            scope=MemoryScope.AGENT,
            scope_id=AGENT,
            status=MemoryStatus.ACTIVE,
            content_hash=content_hash("Ship on Tuesdays."),
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="ship on tuesdays"), source(), AGENT_ACTOR, [existing]
        )
        assert decision.outcome == "duplicate"
        assert decision.duplicate_of == existing.id

    def test_duplicate_in_other_scope_does_not_count(self) -> None:
        existing = ExistingRecord(
            id=new_uuid7(),
            scope=MemoryScope.AGENT,
            scope_id=new_uuid7(),  # another agent
            status=MemoryStatus.ACTIVE,
            content_hash=content_hash("Ship on Tuesdays."),
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="Ship on Tuesdays."), source(), AGENT_ACTOR, [existing]
        )
        assert decision.outcome == "activate"

    def test_forgotten_record_does_not_block_new_memory(self) -> None:
        existing = ExistingRecord(
            id=new_uuid7(),
            scope=MemoryScope.AGENT,
            scope_id=AGENT,
            status=MemoryStatus.FORGOTTEN,
            content_hash=content_hash("Ship on Tuesdays."),
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="Ship on Tuesdays."), source(), AGENT_ACTOR, [existing]
        )
        assert decision.outcome == "activate"


class TestContradiction:
    def test_conflicting_value_for_same_subject_marks_both_contested(self) -> None:
        existing = ExistingRecord(
            id=new_uuid7(),
            scope=MemoryScope.AGENT,
            scope_id=AGENT,
            status=MemoryStatus.ACTIVE,
            content_hash=content_hash("Deploys happen on Tuesday."),
            subject="deploy.day",
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="Deploys happen on Thursday.", subject="Deploy.Day"),
            source(),
            AGENT_ACTOR,
            [existing],
        )
        assert decision.outcome == "activate"
        assert decision.status is MemoryStatus.CONTESTED
        assert decision.contested_with == (existing.id,)
        assert "contradiction" in decision.reasons

    def test_no_subject_means_no_contradiction(self) -> None:
        existing = ExistingRecord(
            id=new_uuid7(),
            scope=MemoryScope.AGENT,
            scope_id=AGENT,
            status=MemoryStatus.ACTIVE,
            content_hash=content_hash("Deploys happen on Tuesday."),
            subject=None,
        )
        decision = evaluate_candidate(
            MemoryCandidate(content="Deploys happen on Thursday."),
            source(),
            AGENT_ACTOR,
            [existing],
        )
        assert decision.status is MemoryStatus.ACTIVE
        assert decision.contested_with == ()


class TestPromotion:
    def test_agent_private_auto_activates(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Ava ships on Tuesdays."), source(), AGENT_ACTOR
        )
        assert decision.status is MemoryStatus.ACTIVE
        assert decision.scope_id == AGENT

    def test_team_activates_when_source_team_visible(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Standups happen at nine.", requested_scope=MemoryScope.TEAM),
            source(MemoryScope.TEAM),
            AGENT_ACTOR,
        )
        assert decision.status is MemoryStatus.ACTIVE
        assert decision.scope_id == TEAM
        assert "team_source_visible" in decision.reasons

    def test_team_scope_without_team_is_rejected(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Standups happen at nine.", requested_scope=MemoryScope.TEAM),
            source(MemoryScope.TEAM, team_id=None),
            AGENT_ACTOR,
        )
        assert decision.outcome == "reject"
        assert "no_team_for_scope" in decision.reasons

    def test_workspace_promotion_stays_proposed(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(
                content="The company is fully remote.", requested_scope=MemoryScope.WORKSPACE
            ),
            source(MemoryScope.WORKSPACE),
            AGENT_ACTOR,
        )
        assert decision.outcome == "propose"
        assert decision.status is MemoryStatus.PROPOSED
        assert "workspace_promotion_requires_review" in decision.reasons

    def test_evidence_is_content_free(self) -> None:
        decision = evaluate_candidate(
            MemoryCandidate(content="Top secret roadmap item"), source(), AGENT_ACTOR
        )
        assert "Top secret" not in str(decision.evidence())
