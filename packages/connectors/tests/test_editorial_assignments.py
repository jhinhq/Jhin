"""Assignment authority, immutable evidence, and optimistic brief updates."""

from dataclasses import replace

import pytest

from jhin_connectors.ghost.assignments import (
    AssignmentCreateInput,
    AssignmentReviseInput,
    EditorialBrief,
    create_assignment,
    revise_assignment,
)
from jhin_connectors.ghost.client import GhostApiError
from jhin_db.models import Agent, Task
from jhin_domain import new_uuid7


@pytest.fixture
async def assignment_context(context, make_connection, workspace):
    writer = Agent(id=context.agent_id, workspace_id=workspace.id, name="Writer", slug="writer")
    publisher = Agent(workspace_id=workspace.id, name="Ashley", slug="ashley")
    context.session.add_all([writer, publisher])
    await context.session.flush()
    task = Task(
        id=context.task_id,
        workspace_id=workspace.id,
        title="Article",
        assigned_agent_id=writer.id,
        correlation_id=new_uuid7(),
    )
    context.session.add(task)
    conn = await make_connection(
        workspace,
        connector_type="ghost",
        config={"admin_url": "https://blog.example.test", "publisher_agent_id": str(publisher.id)},
    )
    return context, conn, publisher


async def test_assignment_create_is_idempotent_draft_only_and_revision_checked(assignment_context):
    ctx, conn, _ = assignment_context
    data = AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief(audience="Fans"))
    initial = await create_assignment(ctx, data)
    again = await create_assignment(ctx, data)
    assert again.assignment_id == initial.assignment_id and initial.release_intent == "draft_only"
    revised = await revise_assignment(
        ctx,
        AssignmentReviseInput(
            connection_id=str(conn.id),
            assignment_id=initial.assignment_id,
            expected_version=initial.version,
            brief=EditorialBrief(audience="New fans"),
        ),
    )
    assert revised.editorial_version == 2 and revised.brief_version == 2
    with pytest.raises(GhostApiError, match="version changed"):
        await revise_assignment(
            ctx,
            AssignmentReviseInput(
                connection_id=str(conn.id),
                assignment_id=initial.assignment_id,
                expected_version=initial.version,
                brief=EditorialBrief(audience="Stale"),
            ),
        )


async def test_assignment_cannot_be_read_by_renamed_or_foreign_actor(assignment_context):
    from jhin_connectors.ghost.assignments import AssignmentReadInput, read_assignment

    ctx, conn, _ = assignment_context
    initial = await create_assignment(
        ctx, AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief())
    )
    for outsider in (
        replace(ctx, agent_id=new_uuid7(), agent_name="Ashley"),
        replace(ctx, workspace_id=new_uuid7()),
    ):
        with pytest.raises(GhostApiError, match="unavailable"):
            await read_assignment(
                outsider,
                AssignmentReadInput(
                    connection_id=str(conn.id), assignment_id=initial.assignment_id
                ),
            )


async def test_forged_evidence_tool_receipt_is_rejected(assignment_context):
    from jhin_connectors.ghost.assignments import AssignmentEvidenceInput, attach_evidence

    ctx, conn, _ = assignment_context
    initial = await create_assignment(
        ctx, AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief())
    )
    with pytest.raises(GhostApiError, match="evidence"):
        await attach_evidence(
            ctx,
            AssignmentEvidenceInput(
                connection_id=str(conn.id),
                assignment_id=initial.assignment_id,
                expected_version=initial.version,
                evidence_tool_call_ids=[new_uuid7()],
            ),
        )


async def test_completed_evidence_is_bound_and_changed_receipt_invalidates_package(
    assignment_context,
):
    from uuid import UUID

    from jhin_connectors.ghost.assignments import (
        AssignmentEvidenceInput,
        attach_evidence,
        ensure_package,
        evidence_snapshot,
    )
    from jhin_db.models import AgentRun, ToolCall
    from jhin_db.models.editorial import EditorialAssignment

    ctx, conn, _ = assignment_context
    initial = await create_assignment(
        ctx, AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief())
    )
    run = AgentRun(
        id=ctx.run_id, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, task_id=ctx.task_id
    )
    ctx.session.add(run)
    await ctx.session.flush()
    call = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=ctx.agent_id,
        tool_name="web.fetch",
        status="completed",
        sanitized_input_json={"url": "https://example.test/source"},
        sanitized_output_json={"text": "Actual retrieved content", "truncated": False},
    )
    ctx.session.add(call)
    await ctx.session.flush()
    attached = await attach_evidence(
        ctx,
        AssignmentEvidenceInput(
            connection_id=str(conn.id),
            assignment_id=initial.assignment_id,
            expected_version=initial.version,
            evidence_tool_call_ids=[call.id],
        ),
    )
    row = await ctx.session.get(EditorialAssignment, UUID(initial.assignment_id))
    package = await ensure_package(ctx, row, "f" * 64, "https://blog.example.test")
    assert attached.editorial_version == 2
    assert package.manifest_json["evidence"][0]["output"]["text"] == "Actual retrieved content"
    call.sanitized_output_json = {"text": "Changed evidence"}
    fresh = await evidence_snapshot(ctx, row, row.evidence_tool_call_ids)
    assert fresh != package.manifest_json["evidence"]


async def test_new_assignment_missing_research_cannot_request_review(
    assignment_context, monkeypatch
):
    from uuid import UUID

    from jhin_connectors.ghost.schemas import ReviewRequestInput
    from jhin_connectors.ghost.tools import _review_request
    from jhin_db.models.editorial import EditorialAssignment

    ctx, conn, _ = assignment_context
    initial = await create_assignment(
        ctx, AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief())
    )
    row = await ctx.session.get(EditorialAssignment, UUID(initial.assignment_id))
    row.post_id = "c" * 24
    calls = []

    async def provider(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Provider must not be contacted")

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", provider)
    with pytest.raises(GhostApiError, match="research"):
        await _review_request(
            ctx,
            ReviewRequestInput(
                connection_id=str(conn.id),
                assignment_id=initial.assignment_id,
                expected_editorial_version=1,
                post_id=row.post_id,
                expected_revision="f" * 64,
                summary="Ready",
            ),
        )
    assert calls == []


async def test_archive_receipt_requires_matching_current_complete_corpus(assignment_context):
    from uuid import UUID

    from jhin_connectors.ghost.assignments import (
        AssignmentEvidenceInput,
        attach_evidence,
        require_research,
    )
    from jhin_db.models import AgentRun, ToolCall
    from jhin_db.models.blog_corpus import BlogCorpusSync
    from jhin_db.models.editorial import EditorialAssignment

    ctx, conn, _ = assignment_context
    initial = await create_assignment(
        ctx, AssignmentCreateInput(connection_id=str(conn.id), brief=EditorialBrief())
    )
    row = await ctx.session.get(EditorialAssignment, UUID(initial.assignment_id))
    run = AgentRun(
        id=ctx.run_id, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id, task_id=ctx.task_id
    )
    ctx.session.add(run)
    await ctx.session.flush()
    sync = BlogCorpusSync(
        workspace_id=ctx.workspace_id,
        connection_id=conn.id,
        assignment_id=row.id,
        agent_id=ctx.agent_id,
        task_id=ctx.task_id,
        run_id=run.id,
        status="complete",
        discovered=10,
        indexed=10,
        failed=0,
        corpus_hash="a" * 64,
    )
    ctx.session.add(sync)
    await ctx.session.flush()
    source = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=ctx.agent_id,
        tool_name="web.fetch",
        status="completed",
        sanitized_output_json={"text": "Actual source"},
    )
    archive = ToolCall(
        workspace_id=ctx.workspace_id,
        run_id=run.id,
        agent_id=ctx.agent_id,
        tool_name="ghost.archive.status",
        status="completed",
        sanitized_input_json={"assignment_id": str(row.id)},
        sanitized_output_json={
            "sync_id": str(sync.id),
            "status": "complete",
            "discovered": 10,
            "indexed": 10,
            "failed": 0,
            "corpus_hash": "a" * 64,
        },
    )
    ctx.session.add_all([source, archive])
    await ctx.session.flush()
    await attach_evidence(
        ctx,
        AssignmentEvidenceInput(
            connection_id=str(conn.id),
            assignment_id=str(row.id),
            expected_version=row.version,
            evidence_tool_call_ids=[source.id, archive.id],
        ),
    )
    await require_research(ctx, row)
    sync.corpus_hash = "b" * 64
    with pytest.raises(GhostApiError, match="research"):
        await require_research(ctx, row)
