"""Successor chat episodes retain only a verified, ongoing editorial assignment."""

from __future__ import annotations

from typing import Any

import pytest
from apps.api.tests.test_conversations_unit import FakeTemporal, start
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.conversations import service
from jhin_api.deps import WorkspaceContext
from jhin_api.tasks import service as tasks_service
from jhin_db.models import Agent, Connection, Task
from jhin_db.models.editorial import EditorialAssignment, GhostEditorialReview
from jhin_domain import new_uuid7


@pytest.fixture
async def editorial(session: AsyncSession, admin_ctx: WorkspaceContext) -> tuple[Any, ...]:
    writer = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    publisher = Agent(workspace_id=admin_ctx.workspace_id, name="Director", slug="director")
    connection = Connection(
        workspace_id=admin_ctx.workspace_id,
        connector_type="ghost",
        name="Blog",
        auth_type="api_key",
    )
    session.add_all([writer, publisher, connection])
    await session.flush()
    temporal = FakeTemporal()
    conversation, first = await start(session, admin_ctx, temporal, writer, "Draft an article")
    first.task.state = "completed"
    assignment = EditorialAssignment(
        workspace_id=admin_ctx.workspace_id,
        connection_id=connection.id,
        writer_agent_id=writer.id,
        publisher_agent_id=publisher.id,
        task_id=first.task.id,
        conversation_id=conversation.id,
        phase="brief",
        release_intent="draft_only",
    )
    session.add(assignment)
    await session.commit()
    return conversation, first, assignment, temporal, writer


async def successor(
    session: AsyncSession,
    ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    operation: str,
    **kwargs: Any,
) -> Task:
    conversation, first, _, temporal, _ = editorial
    if operation == "resume":
        first.task.state = "failed"
        await session.commit()
        result = await service.resume_conversation(
            session, ctx, temporal, conversation.id, request_id=new_uuid7(), ip_hash="h"
        )
    else:
        result = await service.send_turn(
            session,
            ctx,
            temporal,
            conversation.id,
            text="Continue the article",
            client_turn_id="editorial-next",
            request_id=new_uuid7(),
            ip_hash="h",
            **kwargs,
        )
    return result.task


@pytest.mark.parametrize("operation", ["turn", "resume"])
@pytest.mark.parametrize(
    "phase", ["brief", "draft", "blocked", "awaiting_review", "changes_requested"]
)
async def test_successor_recovers_original_assignment_without_task_metadata(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    operation: str,
    phase: str,
) -> None:
    _, first, assignment, temporal, _ = editorial
    assignment.phase = phase
    await session.commit()
    original_metadata = dict(first.task.metadata_json)

    task = await successor(session, admin_ctx, editorial, operation)

    assert task.id != first.task.id
    assert task.metadata_json["editorial_assignment_id"] == str(assignment.id)
    assert assignment.task_id == first.task.id
    assert assignment.release_intent == "draft_only"
    assert "editorial_assignment_id" not in original_metadata
    assert "editorial_assignment_id" not in first.task.metadata_json
    assert "work_request" not in task.metadata_json
    assert len(temporal.started) == 2


async def test_later_successor_keeps_verified_assignment_link(
    session: AsyncSession, admin_ctx: WorkspaceContext, editorial: tuple[Any, ...]
) -> None:
    conversation, first, assignment, temporal, _ = editorial
    second = await successor(session, admin_ctx, editorial, "turn")
    second.state = "completed"
    await session.commit()
    third = await service.send_turn(
        session,
        admin_ctx,
        temporal,
        conversation.id,
        text="Continue reviewing sources",
        client_turn_id="third",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert third.task.metadata_json["editorial_assignment_id"] == str(assignment.id)
    assert assignment.task_id == first.task.id


@pytest.mark.parametrize("delivery", ["queue", "late_signal"])
async def test_queued_and_late_successor_preserve_assignment(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
) -> None:
    _, first, assignment, _, _ = editorial
    first.task.state = "running"
    await session.commit()
    if delivery == "late_signal":

        async def finished(*args: Any, **kwargs: Any) -> None:
            raise HTTPException(409, "Task finished before signal")

        monkeypatch.setattr(tasks_service, "signal_task", finished)
    task = await successor(
        session, admin_ctx, editorial, "turn", delivery="queue" if delivery == "queue" else "auto"
    )
    assert task.id != first.task.id
    assert task.metadata_json["editorial_assignment_id"] == str(assignment.id)


@pytest.mark.parametrize("operation", ["turn", "resume"])
@pytest.mark.parametrize(
    "mismatch",
    [
        "workspace",
        "conversation",
        "writer",
        "reassigned_predecessor",
        "unrelated_task",
        "stopped_predecessor",
        "stale_link",
        "conflicting_link",
    ],
)
async def test_successor_does_not_inherit_untrusted_or_reassigned_lineage(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    operation: str,
    mismatch: str,
) -> None:
    _, first, assignment, _, _ = editorial
    if mismatch in {"workspace", "conversation"}:
        setattr(assignment, f"{mismatch}_id", new_uuid7())
    elif mismatch == "writer":
        assignment.writer_agent_id = assignment.publisher_agent_id
    elif mismatch == "reassigned_predecessor":
        first.task.assigned_agent_id = assignment.publisher_agent_id
    elif mismatch == "unrelated_task":
        assignment.task_id = new_uuid7()
    elif mismatch == "stopped_predecessor":
        first.task.metadata_json = {**first.task.metadata_json, "stop_requested_at": "stopped"}
    elif mismatch == "stale_link":
        first.task.metadata_json = {
            **first.task.metadata_json,
            "editorial_assignment_id": str(new_uuid7()),
        }
    elif mismatch == "conflicting_link":
        first.task.metadata_json = {
            **first.task.metadata_json,
            "editorial_assignment_id": str(assignment.id),
            "work_request": {"editorial_assignment_id": str(new_uuid7())},
        }
    await session.commit()
    task = await successor(session, admin_ctx, editorial, operation)
    assert "editorial_assignment_id" not in task.metadata_json


@pytest.mark.parametrize(
    "phase", ["cancelled", "completed", "published", "publishing", "future_phase"]
)
async def test_successor_does_not_reopen_ineligible_assignment_phase(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    phase: str,
) -> None:
    assignment = editorial[2]
    assignment.phase = phase
    await session.commit()
    task = await successor(session, admin_ctx, editorial, "turn")
    assert "editorial_assignment_id" not in task.metadata_json


@pytest.mark.parametrize("operation", ["turn", "resume"])
async def test_multiple_open_assignments_do_not_autoattach(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    operation: str,
) -> None:
    _, first, assignment, _, _ = editorial
    session.add(
        EditorialAssignment(
            workspace_id=assignment.workspace_id,
            connection_id=assignment.connection_id,
            writer_agent_id=assignment.writer_agent_id,
            publisher_agent_id=assignment.publisher_agent_id,
            conversation_id=assignment.conversation_id,
            task_id=first.task.id,
        )
    )
    await session.commit()
    task = await successor(session, admin_ctx, editorial, operation)
    assert "editorial_assignment_id" not in task.metadata_json


@pytest.mark.parametrize("status", ["approved", "publishing", "published", "uncertain"])
async def test_successor_does_not_reopen_terminal_or_uncertain_review(
    session: AsyncSession,
    admin_ctx: WorkspaceContext,
    editorial: tuple[Any, ...],
    status: str,
) -> None:
    assignment = editorial[2]
    assignment.phase = "draft"
    session.add(
        GhostEditorialReview(
            workspace_id=assignment.workspace_id,
            connection_id=assignment.connection_id,
            post_id="a" * 24,
            revision="a" * 64,
            admin_url="https://blog.example",
            provider_updated_at="2026-09-16T00:00:00Z",
            snapshot_json={},
            author_agent_id=assignment.writer_agent_id,
            publisher_agent_id=assignment.publisher_agent_id,
            assignment_id=assignment.id,
            assignment_editorial_version=assignment.editorial_version,
            status=status,
        )
    )
    await session.commit()
    task = await successor(session, admin_ctx, editorial, "turn")
    assert "editorial_assignment_id" not in task.metadata_json
