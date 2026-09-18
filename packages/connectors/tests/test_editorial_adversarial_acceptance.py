"""Adversarial acceptance through actual editorial and owner-review boundaries.

Only the Ghost HTTP transport is doubled. Approval/read receipts, packages,
generic work reviews, evidence queries, and revision limits use production code.
"""

from dataclasses import replace
from importlib import import_module
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from jhin_connectors.ghost.client import GhostApiError, post_revision
from jhin_connectors.ghost.schemas import (
    DraftUpdateInput,
    PostReadInput,
    PublishInput,
    ReviewDecisionInput,
    ReviewReadInput,
    ReviewRequestInput,
)
from jhin_connectors.ghost.tools import (
    _draft_update,
    _post_read,
    _publish,
    _review_decide,
    _review_read,
    _review_request,
)
from jhin_db.models import Agent, AgentRun, GhostEditorialReview, Task, ToolCall, User, WorkReview
from jhin_db.models.blog_corpus import BlogCorpusSync
from jhin_domain import WorkspaceRole, new_uuid7

cases = import_module("packages.connectors.tests.test_ghost_editorial")
editorial = cases.editorial


def review_input(world: Any) -> ReviewRequestInput:
    _, connection, _, post, _ = world
    assignment = connection._test_assignment
    return ReviewRequestInput(
        connection_id=str(connection.id),
        assignment_id=str(assignment.id),
        expected_editorial_version=assignment.editorial_version,
        post_id=post["id"],
        expected_revision=post_revision(post),
        summary="Review the evidence and return actionable issue IDs.",
    )


async def read_and_decide(world: Any, review_id: str, verdict: str) -> None:
    ctx, connection, publisher, post, _ = world
    director = replace(ctx, agent_id=publisher.id)
    await _post_read(director, PostReadInput(connection_id=str(connection.id), post_id=post["id"]))
    offset = 0
    while True:
        package = await _review_read(
            director,
            ReviewReadInput(connection_id=str(connection.id), review_id=review_id, offset=offset),
        )
        if package.next_offset is None:
            break
        offset = package.next_offset
    await _review_decide(
        director,
        ReviewDecisionInput(
            connection_id=str(connection.id),
            review_id=review_id,
            verdict=verdict,
            feedback="ISSUE-SOURCE-1: Supply the original source for the numerical claim.",
        ),
    )
    await ctx.session.commit()


async def seed_research(world: Any) -> ToolCall:
    ctx, connection, _, _, _ = world
    assignment = connection._test_assignment
    task = Task(
        id=ctx.task_id,
        workspace_id=ctx.workspace_id,
        title="Research this article",
        assigned_agent_id=ctx.agent_id,
        correlation_id=new_uuid7(),
    )
    ctx.session.add(task)
    await ctx.session.flush()
    run = AgentRun(
        id=ctx.run_id, workspace_id=ctx.workspace_id, task_id=task.id, agent_id=ctx.agent_id
    )
    ctx.session.add(run)
    await ctx.session.flush()
    assignment.task_id = task.id
    assignment.decision_provenance = {"research_required": True}
    corpus = BlogCorpusSync(
        workspace_id=ctx.workspace_id,
        connection_id=connection.id,
        assignment_id=assignment.id,
        agent_id=ctx.agent_id,
        task_id=task.id,
        run_id=run.id,
        status="complete",
        discovered=1,
        indexed=1,
        failed=0,
        corpus_hash="a" * 64,
    )
    ctx.session.add(corpus)
    await ctx.session.flush()
    source = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=ctx.agent_id,
        tool_name="web.fetch",
        status="completed",
        sanitized_input_json={"url": "https://source.example.test/research"},
        sanitized_output_json={"text": "Original research result: 12 participants."},
    )
    archive = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=ctx.agent_id,
        tool_name="ghost.archive.status",
        status="completed",
        sanitized_input_json={"assignment_id": str(assignment.id)},
        sanitized_output_json={
            "sync_id": str(corpus.id),
            "status": "complete",
            "discovered": 1,
            "indexed": 1,
            "failed": 0,
            "corpus_hash": corpus.corpus_hash,
        },
    )
    ctx.session.add_all([source, archive])
    await ctx.session.flush()
    assignment.evidence_tool_call_ids = [str(source.id), str(archive.id)]
    await ctx.session.commit()
    return source


async def test_owner_generic_review_override_does_not_approve_ghost(editorial: Any) -> None:
    from jhin_api.coordination.service import decide_review
    from jhin_api.deps import WorkspaceContext

    ctx, connection, publisher, post, calls = editorial
    output = await _review_request(ctx, review_input(editorial))
    review = await ctx.session.get(GhostEditorialReview, UUID(output.review_id))
    assert review is not None and review.work_review_id is not None
    owner = User(email="owner@editorial.test", display_name="Owner", password_hash="fixture")
    ctx.session.add(owner)
    await ctx.session.commit()
    generic = await decide_review(
        ctx.session,
        WorkspaceContext(user=owner, workspace_id=ctx.workspace_id, role=WorkspaceRole.OWNER),
        review.work_review_id,
        verdict="approve",
        feedback="Owner overrides the generic task gate.",
        request_id=new_uuid7(),
        ip_hash="fixture",
    )
    assert generic.status == "approved" and generic.decided_by_user_id == owner.id
    await ctx.session.refresh(review)
    assert review.status == "pending"
    calls.clear()
    with pytest.raises(GhostApiError) as denied:
        await _publish(
            replace(ctx, agent_id=publisher.id),
            PublishInput(connection_id=str(connection.id), review_id=str(review.id)),
        )
    assert denied.value.code == "ghost_review_not_publishable"
    assert calls == [] and post["status"] == "draft"


async def test_agent_renamed_ashley_cannot_publish_approved_review(editorial: Any) -> None:
    ctx, connection, publisher, post, calls = editorial
    publisher.name = "Ashley"
    impersonator = Agent(
        workspace_id=ctx.workspace_id, name="Other Agent", slug="other-agent", status="active"
    )
    ctx.session.add(impersonator)
    await ctx.session.flush()
    review = await cases.make_review(editorial)
    await read_and_decide(editorial, str(review.id), "approved")
    # Names are unique; renaming the real publisher frees the old display name
    # without changing the immutable publisher IDs on the connection/review.
    publisher.name = "Original Publisher"
    await ctx.session.flush()
    impersonator.name = "Ashley"
    await ctx.session.commit()
    calls.clear()
    with pytest.raises(GhostApiError) as denied:
        await _publish(
            replace(ctx, agent_id=impersonator.id, agent_name="Ashley"),
            PublishInput(connection_id=str(connection.id), review_id=str(review.id)),
        )
    assert denied.value.code == "ghost_publisher_only"
    assert calls == [] and post["status"] == "draft" and review.status == "approved"


@pytest.mark.parametrize(
    "change",
    ["feature_image_caption", "source", "meta_title", "meta_description", "authors", "intent"],
)
async def test_metadata_drift_invalidates_approved_unchanged_body(
    editorial: Any, change: str
) -> None:
    ctx, connection, publisher, post, calls = editorial
    source = await seed_research(editorial)
    review = await cases.make_review(editorial)
    await read_and_decide(editorial, str(review.id), "approved")
    original_body = post["html"]
    if change == "source":
        source.sanitized_output_json = {"text": "Corrected research result: 120 participants."}
    elif change == "intent":
        connection._test_assignment.release_intent = "draft_only"
    elif change == "authors":
        post["authors"] = [{"id": "d" * 24, "name": "Different Author"}]
    else:
        post[change] = "Changed metadata after approval"
    await ctx.session.commit()
    calls.clear()
    with pytest.raises(GhostApiError) as denied:
        await _publish(
            replace(
                ctx,
                agent_id=publisher.id,
                tool_call_id=new_uuid7(),
                session_factory=async_sessionmaker(ctx.session.bind, expire_on_commit=False),
            ),
            PublishInput(connection_id=str(connection.id), review_id=str(review.id)),
        )
    assert denied.value.code in {
        "ghost_package_stale",
        "ghost_draft_only",
        "ghost_revision_conflict",
    }
    assert post["html"] == original_body and post["status"] == "draft"
    assert all(method == "GET" for method, _, _ in calls)


async def test_image_required_brief_refuses_review_until_explicit_amendment(editorial: Any) -> None:
    from jhin_connectors.ghost.assignments import (
        AssignmentReviseInput,
        EditorialBrief,
        revise_assignment,
    )

    ctx, connection, _, post, calls = editorial
    await seed_research(editorial)
    assignment = connection._test_assignment
    assignment.brief_json = EditorialBrief(image_mode="cover").model_dump(mode="json")
    await ctx.session.commit()
    with pytest.raises(GhostApiError) as denied:
        await _review_request(ctx, review_input(editorial))
    assert denied.value.code == "ghost_image_evidence_incomplete"
    assert not list(await ctx.session.scalars(select(GhostEditorialReview)))
    assert post["status"] == "draft" and all(method == "GET" for method, _, _ in calls)
    await revise_assignment(
        ctx,
        AssignmentReviseInput(
            connection_id=str(connection.id),
            assignment_id=str(assignment.id),
            expected_version=assignment.version,
            brief=EditorialBrief(image_mode="none"),
        ),
    )
    output = await _review_request(ctx, review_input(editorial))
    assert output.status == "pending" and output.work_request_id


async def test_three_changes_requested_rounds_block_fourth_without_forced_approval(
    editorial: Any,
) -> None:
    ctx, connection, _, post, calls = editorial
    assignment = connection._test_assignment
    reviews: list[GhostEditorialReview] = []
    for round_number in range(1, 4):
        output = await _review_request(ctx, review_input(editorial))
        assert output.revision_round == round_number
        row = await ctx.session.get(GhostEditorialReview, UUID(output.review_id))
        assert row is not None
        assert row.prior_review_id == (reviews[-1].id if reviews else None)
        await read_and_decide(editorial, output.review_id, "changes_requested")
        reviews.append(row)
        await _draft_update(
            ctx,
            DraftUpdateInput(
                connection_id=str(connection.id),
                assignment_id=str(assignment.id),
                expected_editorial_version=assignment.editorial_version,
                post_id=post["id"],
                expected_updated_at=post["updated_at"],
                title=post["title"],
                slug=post["slug"],
                html=f"<p>Revision {round_number}, source issue still unresolved.</p>",
            ),
        )
        await ctx.session.commit()
    retained_body = post["html"]
    calls.clear()
    with pytest.raises(GhostApiError) as denied:
        await _review_request(ctx, review_input(editorial))
    assert denied.value.code == "ghost_revision_limit"
    await ctx.session.commit()
    await ctx.session.refresh(assignment)
    assert assignment.phase == "blocked"
    assert assignment.blocked_reason == "Three revision rounds require editorial escalation"
    assert len(list(await ctx.session.scalars(select(GhostEditorialReview)))) == 3
    assert all(row.status == "changes_requested" for row in reviews)
    assert all(row.status != "approved" for row in await ctx.session.scalars(select(WorkReview)))
    assert post["status"] == "draft" and post["html"] == retained_body
    assert all(method == "GET" for method, _, _ in calls)
