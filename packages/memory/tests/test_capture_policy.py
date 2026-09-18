"""Prospective capture uses stored authority and exact, live source boundaries."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentTeamMembership,
    Conversation,
    Message,
    Task,
    Team,
    User,
    Workspace,
    WorkspaceMembership,
)
from jhin_db.models.memory_capture import MemoryCapturePolicy
from jhin_domain import ActorType, MemoryScope, new_uuid7
from jhin_memory import (
    ActorFacts,
    MemoryCandidate,
    SourceFacts,
    apply_candidates,
    derive_source_facts,
    evaluate_candidate,
)


@pytest.fixture
async def capture(session: AsyncSession):
    now = datetime.now(UTC)
    ws = Workspace(name="Capture", slug=str(new_uuid7()))
    user = User(email=f"{new_uuid7()}@example.test", display_name="Owner", password_hash="unused")
    session.add_all([ws, user])
    await session.flush()
    primary = Team(workspace_id=ws.id, name="Primary")
    team = Team(workspace_id=ws.id, name="Marketing")
    session.add_all([primary, team])
    await session.flush()
    agent = Agent(workspace_id=ws.id, team_id=primary.id, name="Writer", slug="writer")
    session.add(agent)
    await session.flush()
    membership = AgentTeamMembership(workspace_id=ws.id, agent_id=agent.id, team_id=team.id)
    conversation = Conversation(
        workspace_id=ws.id,
        created_by_user_id=user.id,
        title="Editorial",
        primary_agent_id=agent.id,
        last_activity_at=now,
    )
    session.add_all(
        [
            membership,
            conversation,
            WorkspaceMembership(workspace_id=ws.id, user_id=user.id, role="owner"),
            AgentCapabilityGrant(
                workspace_id=ws.id,
                agent_id=agent.id,
                capability="memory.propose",
                effect="allow",
                scope_json={},
            ),
        ]
    )
    await session.flush()
    task = Task(
        workspace_id=ws.id,
        title="Editorial",
        assigned_agent_id=agent.id,
        conversation_id=conversation.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    message = Message(
        workspace_id=ws.id,
        conversation_id=conversation.id,
        task_id=task.id,
        sender_type="user",
        sender_id=user.id,
        recipient_type="agent",
        recipient_id=agent.id,
        message_type="instruction",
        visibility="visible",
        content_json={"text": "Our blog uses friendly, practical language."},
        created_at=now,
    )
    policy = MemoryCapturePolicy(
        workspace_id=ws.id,
        scope="team",
        scope_id=team.id,
        granted_by_user_id=user.id,
        allowed_classes_json=["editorial_style"],
        actor_ids_json=[str(agent.id)],
        source_user_id=user.id,
        source_conversation_id=conversation.id,
        effective_from=now - timedelta(seconds=5),
    )
    session.add_all([message, policy])
    await session.flush()
    return agent, team, task, message, policy, membership


async def _capture(session, capture, **changes):
    agent, team, task, message, _, _ = capture
    source = await derive_source_facts(
        session, workspace_id=agent.workspace_id, agent_id=agent.id, task_id=task.id
    )
    return await apply_candidates(
        session,
        candidates=[
            MemoryCandidate(
                content=message.content_json["text"],
                requested_scope=MemoryScope.TEAM,
                scope_id=team.id,
                source_message_id=message.id,
                capture_class="editorial_style",
            ).model_copy(update=changes)
        ],
        source=source,
        actor=ActorFacts(actor_type=ActorType.AGENT, actor_id=agent.id),
        require_evidence=True,
    )


async def test_standing_capture_reuses_exact_nonprimary_destination(session, capture):
    first = await _capture(session, capture)
    assert first.created[0].scope_id == capture[1].id
    assert first.created[0].source_message_id == capture[3].id
    assert first.created[0].policy_json["storage_decision"]["authority_id"] == str(capture[4].id)
    second = await _capture(session, capture)
    assert second.duplicates == 1
    assert not second.created


async def test_private_source_cannot_be_promoted_by_dropping_privacy_qualifier(session, capture):
    capture[3].content_json = {"text": "Keep this private: our blog uses friendly language."}
    await session.flush()
    result = await _capture(session, capture, content="Our blog uses friendly language.")
    assert not result.created


@pytest.mark.parametrize(
    "invalid", [None, "pending", "stale", "wrong_actor", "company", "private", "old"]
)
async def test_editorial_lesson_requires_current_approved_shared_review(session, capture, invalid):
    import hashlib
    import json

    from jhin_db.models.editorial import (
        EditorialAssignment,
        EditorialReviewPackage,
        GhostEditorialReview,
    )

    agent, team, task, _, policy, _ = capture
    reviewer = Agent(
        workspace_id=agent.workspace_id, team_id=team.id, name="Director", slug="director"
    )
    session.add(reviewer)
    await session.flush()
    assignment = EditorialAssignment(
        workspace_id=agent.workspace_id,
        connection_id=new_uuid7(),
        writer_agent_id=agent.id,
        publisher_agent_id=reviewer.id,
        team_id=team.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        editorial_version=1,
    )
    session.add(assignment)
    await session.flush()
    manifest = {"provider_revision": "provider"}
    revision = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    package = EditorialReviewPackage(
        workspace_id=agent.workspace_id,
        assignment_id=assignment.id,
        editorial_version=1,
        revision=revision,
        manifest_json=manifest,
    )
    session.add(package)
    await session.flush()
    review = GhostEditorialReview(
        workspace_id=agent.workspace_id,
        connection_id=assignment.connection_id,
        post_id="a" * 24,
        revision="provider",
        admin_url="https://blog.example.test",
        provider_updated_at="2026-09-15",
        snapshot_json={},
        author_agent_id=agent.id,
        publisher_agent_id=reviewer.id,
        assignment_id=assignment.id,
        package_id=package.id,
        assignment_editorial_version=1,
        status="approved",
        decided_at=datetime.now(UTC),
        feedback="Always cite original research in explanatory articles.",
    )
    policy.allowed_classes_json = ["editorial_lesson"]
    policy.allowed_source_agent_ids_json = [str(reviewer.id)]
    if invalid == "pending":
        review.status = "pending"
    elif invalid == "stale":
        assignment.editorial_version = 2
    elif invalid == "wrong_actor":
        policy.allowed_source_agent_ids_json = [str(agent.id)]
    elif invalid == "private":
        review.feedback = "Keep this private: " + review.feedback
    elif invalid == "old":
        review.decided_at = policy.effective_from - timedelta(seconds=1)
    session.add(review)
    await session.flush()
    result = await _capture(
        session,
        capture,
        content="Always cite original research in explanatory articles.",
        capture_class="editorial_lesson",
        source_message_id=None,
        source_review_id=review.id,
        requested_scope=MemoryScope.WORKSPACE if invalid == "company" else MemoryScope.TEAM,
        scope_id=agent.workspace_id if invalid == "company" else team.id,
    )
    assert bool(result.created) is (invalid is None)
    if result.created:
        assert result.created[0].policy_json["evidence"]["review_id"] == str(review.id)


@pytest.mark.parametrize(
    "change", ["old", "revoked", "expired", "left", "class", "private", "source", "deny"]
)
async def test_capture_rejects_stale_or_ineligible_authority(session, capture, change):
    agent, _, _, message, policy, membership = capture
    if change == "old":
        message.created_at = policy.effective_from - timedelta(seconds=1)
    elif change == "revoked":
        policy.revoked_at = datetime.now(UTC)
    elif change == "expired":
        policy.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "left":
        membership.left_at = datetime.now(UTC)
    elif change == "class":
        policy.allowed_classes_json = ["company_fact"]
    elif change == "private":
        message.content_json = {"text": "Keep this private: our blog uses friendly language."}
    elif change == "source":
        message.sender_id = new_uuid7()
    else:
        session.add(
            AgentCapabilityGrant(
                workspace_id=agent.workspace_id,
                agent_id=agent.id,
                capability="memory.propose",
                effect="deny",
                scope_json={},
            )
        )
    await session.flush()
    result = await _capture(session, capture)
    assert not result.created
    assert result.rejected == 1


def test_proposal_can_name_exact_team_and_source() -> None:
    assert {"scope_id", "source_message_id", "capture_class"} <= MemoryCandidate.model_fields.keys()


def test_scope_destination_cannot_be_changed_laterally_without_authority() -> None:
    source = SourceFacts(
        workspace_id=new_uuid7(),
        agent_id=new_uuid7(),
        team_id=new_uuid7(),
        visibility=MemoryScope.TEAM,
    )
    candidate = MemoryCandidate.model_construct(
        content="Our blog uses friendly, practical language.",
        requested_scope=MemoryScope.TEAM,
        scope_id=new_uuid7(),
    )
    decision = evaluate_candidate(candidate, source, ActorFacts(actor_type=ActorType.AGENT))
    assert decision.outcome == "reject"
    assert "destination_not_authorized" in decision.reasons
