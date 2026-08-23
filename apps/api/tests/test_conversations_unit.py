"""Conversation service logic: turns, idempotency, listing, activity feed,
and attention — against in-memory SQLite with a fake Temporal client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.conversations import service
from jhin_api.deps import WorkspaceContext
from jhin_api.tasks import service as tasks_service
from jhin_db.models import (
    Agent,
    AgentRun,
    Approval,
    AuditEvent,
    Conversation,
    Message,
    Task,
    ToolCall,
    User,
    WorkReview,
    Workspace,
)
from jhin_domain import (
    ActivityKind,
    AgentStatus,
    ApprovalStatus,
    MessageType,
    MessageVisibility,
    RecipientType,
    ReviewerType,
    RunStatus,
    SenderType,
    TaskState,
    ToolCallStatus,
    WorkReviewStatus,
    WorkspaceRole,
    new_uuid7,
    structured_content,
)

T0 = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class FakeHandle:
    def __init__(self, client: FakeTemporal, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def signal(self, name: str, *args: Any) -> None:
        self._client.signals.append((self._workflow_id, name, args))


class FakeTemporal:
    """Fakes exactly what tasks.service.start_workflow / signal_task use."""

    def __init__(self) -> None:
        self.started: list[tuple[str, Any, str]] = []
        self.signals: list[tuple[str, str, tuple[Any, ...]]] = []

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str) -> None:
        self.started.append((name, arg, id))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


@pytest.fixture
def temporal() -> FakeTemporal:
    return FakeTemporal()


@pytest.fixture
async def agent(session: AsyncSession, admin_ctx: WorkspaceContext) -> Agent:
    row = Agent(workspace_id=admin_ctx.workspace_id, name="Atlas", slug="atlas", role_title="CTO")
    session.add(row)
    await session.flush()
    return row


async def start(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: FakeTemporal,
    agent: Agent,
    text: str = "Plan the Q4 roadmap\nwith details",
    **kwargs: Any,
) -> tuple[Conversation, service.TurnResult]:
    conversation, turn = await service.create_conversation(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        agent_id=agent.id,
        title=kwargs.pop("title", None),
        text=text,
        client_turn_id=kwargs.pop("client_turn_id", None),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert turn is not None
    return conversation, turn


async def turn(
    session: AsyncSession,
    ctx: WorkspaceContext,
    temporal: FakeTemporal,
    conversation_id: UUID,
    text: str,
    client_turn_id: str | None = None,
) -> service.TurnResult:
    return await service.send_turn(
        session,
        ctx,
        temporal,  # type: ignore[arg-type]
        conversation_id,
        text=text,
        client_turn_id=client_turn_id,
        request_id=new_uuid7(),
        ip_hash="h",
    )


async def test_create_with_first_turn_links_task_and_seed_message(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, result = await start(session, admin_ctx, temporal, agent)

    assert conversation.title == "Plan the Q4 roadmap"
    assert conversation.primary_agent_id == agent.id
    assert result.mode == "new_task"
    assert result.task.conversation_id == conversation.id
    assert result.task.metadata_json == {
        "origin": "conversation",
        "conversation_id": str(conversation.id),
    }
    assert result.message.conversation_id == conversation.id
    assert result.message.task_id == result.task.id
    assert result.message.message_type == MessageType.TEXT.value
    assert [wid for _, _, wid in temporal.started] == [f"task-{result.task.id}"]
    assert result.task.temporal_workflow_id == f"task-{result.task.id}"

    actions = set(
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_id == conversation.id)
        )
    )
    assert actions == {"conversation.created", "conversation.turn"}

    detail = await service.get_detail(session, admin_ctx.workspace_id, conversation.id)
    assert detail.agent is not None and detail.agent.name == "Atlas"
    assert [t.id for t in detail.tasks] == [result.task.id]
    assert detail.conversation.active_task_id == result.task.id
    assert detail.conversation.active_task_state == TaskState.QUEUED.value
    assert detail.conversation.last_message_preview == "Plan the Q4 roadmap with details"
    assert detail.conversation.agent_name == "Atlas"
    assert detail.conversation.task_count == 1


async def test_second_turn_while_task_active_is_an_instruction(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, first = await start(session, admin_ctx, temporal, agent)
    second = await turn(session, admin_ctx, temporal, conversation.id, "Also cover hiring")

    assert second.mode == "instruction"
    assert second.task.id == first.task.id
    assert second.message.message_type == MessageType.INSTRUCTION.value
    assert second.message.conversation_id == conversation.id
    assert temporal.signals == [
        (f"task-{first.task.id}", "user_instruction", ("Also cover hiring",))
    ]
    assert len(temporal.started) == 1


async def test_second_turn_after_completion_starts_a_new_task(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, first = await start(session, admin_ctx, temporal, agent)
    first.task.state = TaskState.COMPLETED.value
    await session.commit()

    second = await turn(session, admin_ctx, temporal, conversation.id, "Now the budget")

    assert second.mode == "new_task"
    assert second.task.id != first.task.id
    # Each episode is titled after its own request, not the chat's first message.
    assert second.task.title == "Now the budget"
    assert second.task.description == "Now the budget"
    assert len(temporal.started) == 2
    assert temporal.signals == []
    projected = await service.project_conversation(session, admin_ctx.workspace_id, conversation)
    assert projected.task_count == 2
    assert projected.active_task_id == second.task.id


async def test_client_turn_id_is_idempotent(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, first = await start(session, admin_ctx, temporal, agent, client_turn_id="c-1")
    replay = await turn(session, admin_ctx, temporal, conversation.id, "ignored", "c-1")

    assert replay.message.id == first.message.id
    assert replay.task.id == first.task.id
    assert replay.mode == "new_task"
    assert len(temporal.started) == 1 and temporal.signals == []

    # An instruction replay keeps its original mode too.
    instr = await turn(session, admin_ctx, temporal, conversation.id, "more", "c-2")
    again = await turn(session, admin_ctx, temporal, conversation.id, "more", "c-2")
    assert instr.mode == again.mode == "instruction"
    assert again.message.id == instr.message.id
    assert len(temporal.signals) == 1


async def test_cross_workspace_is_404(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, _ = await start(session, admin_ctx, temporal, agent)
    other_ws = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add(other_ws)
    await session.flush()
    other_ctx = WorkspaceContext(
        user=admin_ctx.user, workspace_id=other_ws.id, role=WorkspaceRole.ADMIN
    )

    with pytest.raises(HTTPException) as excinfo:
        await turn(session, other_ctx, temporal, conversation.id, "hi")
    assert excinfo.value.status_code == 404
    with pytest.raises(HTTPException) as excinfo:
        await service.get_detail(session, other_ws.id, conversation.id)
    assert excinfo.value.status_code == 404
    with pytest.raises(HTTPException) as excinfo:
        await service.list_messages(session, other_ws.id, conversation.id)
    assert excinfo.value.status_code == 404


async def test_archived_conversation_and_paused_agent_are_409(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, _ = await start(session, admin_ctx, temporal, agent)
    await service.update_conversation(
        session,
        admin_ctx,
        conversation.id,
        values={"status": "archived", "pinned": True},
        request_id=new_uuid7(),
        ip_hash="h",
    )
    with pytest.raises(HTTPException) as excinfo:
        await turn(session, admin_ctx, temporal, conversation.id, "hi")
    assert excinfo.value.status_code == 409
    assert "archived" in str(excinfo.value.detail)

    await service.update_conversation(
        session,
        admin_ctx,
        conversation.id,
        values={"status": "active"},
        request_id=new_uuid7(),
        ip_hash="h",
    )
    agent.status = AgentStatus.PAUSED.value
    await session.commit()
    with pytest.raises(HTTPException) as excinfo:
        await turn(session, admin_ctx, temporal, conversation.id, "hi")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "This agent is paused"

    with pytest.raises(HTTPException) as excinfo:
        await service.create_conversation(
            session,
            admin_ctx,
            temporal,  # type: ignore[arg-type]
            agent_id=agent.id,
            title=None,
            text="x",
            client_turn_id=None,
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert excinfo.value.status_code == 409

    archived, total = await service.list_conversations(
        session, admin_ctx.workspace_id, status_filter="archived"
    )
    assert (archived, total) == ([], 0)
    active, total = await service.list_conversations(session, admin_ctx.workspace_id, pinned=True)
    assert [c.id for c in active] == [conversation.id] and total == 1


async def test_message_listing_merges_tasks_in_order_and_hides_internal(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, first = await start(session, admin_ctx, temporal, agent)
    first.message.created_at = at(0)
    ws = admin_ctx.workspace_id
    reply = Message(
        workspace_id=ws,
        task_id=first.task.id,
        sender_type=SenderType.AGENT.value,
        sender_id=agent.id,
        recipient_type=RecipientType.USER.value,
        recipient_id=admin_ctx.user.id,
        content_json={"text": "Here is the plan."},
        created_at=at(1),
    )
    hidden = Message(
        workspace_id=ws,
        task_id=first.task.id,
        sender_type=SenderType.AGENT.value,
        sender_id=agent.id,
        recipient_type=RecipientType.TASK.value,
        message_type=MessageType.TOOL_CALL.value,
        content_json={"text": "secret tool transcript"},
        visibility=MessageVisibility.INTERNAL.value,
        created_at=at(2),
    )
    session.add_all([reply, hidden])
    first.task.state = TaskState.COMPLETED.value
    await session.commit()

    second = await turn(session, admin_ctx, temporal, conversation.id, "Thanks, next?")
    second.message.created_at = at(3)
    await session.commit()

    messages = await service.list_messages(session, ws, conversation.id)
    assert [m.id for m in messages] == [first.message.id, reply.id, second.message.id]
    projected = await service.project_messages(session, ws, messages)
    assert [p.sender_name for p in projected] == ["Admin", "Atlas", "Admin"]
    assert projected[1].agent_id == agent.id and projected[0].agent_id is None

    tail = await service.list_messages(session, ws, conversation.id, after=reply.id)
    assert [m.id for m in tail] == [second.message.id]


async def test_activity_projection_and_conversation_scope(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    ws = admin_ctx.workspace_id
    builder = Agent(workspace_id=ws, name="Bolt", slug="bolt")
    session.add(builder)
    await session.flush()
    conversation, root_turn = await start(session, admin_ctx, temporal, agent)
    root = root_turn.task
    root.created_at = at(0)

    child = Task(
        workspace_id=ws,
        title="Implement the API",
        state=TaskState.RUNNING.value,
        assigned_agent_id=builder.id,
        parent_task_id=root.id,
        correlation_id=root.correlation_id,
        created_at=at(10),
        updated_at=at(10),
    )
    session.add(child)
    await session.flush()
    delegation = Message(
        workspace_id=ws,
        task_id=root.id,
        sender_type=SenderType.AGENT.value,
        sender_id=agent.id,
        recipient_type=RecipientType.AGENT.value,
        recipient_id=builder.id,
        message_type=MessageType.DELEGATION.value,
        content_json=structured_content(
            "Build the API",
            child_task_id=str(child.id),
            target_agent_id=str(builder.id),
            target_agent_name="Bolt",
        ),
        created_at=at(11),
    )
    result = Message(
        workspace_id=ws,
        task_id=child.id,
        sender_type=SenderType.AGENT.value,
        sender_id=builder.id,
        recipient_type=RecipientType.AGENT.value,
        recipient_id=agent.id,
        message_type=MessageType.RESULT.value,
        content_json=structured_content("API shipped", status="done"),
        created_at=at(12),
    )
    approval = Approval(
        workspace_id=ws,
        task_id=child.id,
        requested_by_agent_id=builder.id,
        action_type="github.pr.merge",
        action_payload_sanitized={"input": {"pr": 7}},
        reason="Merge needs a human",
        status=ApprovalStatus.PENDING.value,
        requested_at=at(13),
    )
    session.add_all([delegation, result, approval])
    root.state = TaskState.COMPLETED.value
    root.updated_at = at(20)
    await session.commit()

    # An unrelated task in the workspace must not leak into the conversation feed.
    stray = Task(
        workspace_id=ws,
        title="Stray",
        assigned_agent_id=builder.id,
        correlation_id=new_uuid7(),
        created_at=at(30),
        updated_at=at(30),
    )
    session.add(stray)
    await session.commit()

    feed = await service.list_activity(session, ws, conversation_id=conversation.id)
    by_id = {card.id: card for card in feed.items}
    assert list(by_id) == [
        f"task:{root.id}:completed",
        f"approval:{approval.id}",
        f"msg:{result.id}",
        f"msg:{delegation.id}",
        f"task:{child.id}:started",
        f"task:{root.id}:started",
    ]
    asked = by_id[f"msg:{delegation.id}"]
    assert asked.kind is ActivityKind.ASKED_AGENT
    assert asked.label == "Asked another agent"
    assert asked.actor_agent_name == "Atlas"
    assert asked.target_agent_id == builder.id and asked.target_agent_name == "Bolt"
    assert asked.summary == "Build the API"
    assert asked.conversation_id == conversation.id and asked.root_task_id == root.id

    reported = by_id[f"msg:{result.id}"]
    assert reported.kind is ActivityKind.REPORTED
    assert reported.task_id == child.id
    assert reported.root_task_id == root.id and reported.conversation_id == conversation.id

    finished = by_id[f"task:{root.id}:completed"]
    assert finished.kind is ActivityKind.FINISHED
    assert finished.summary == "Atlas finished “Plan the Q4 roadmap”."
    assert finished.created_at == at(20)

    review = by_id[f"approval:{approval.id}"]
    assert review.kind is ActivityKind.NEEDS_REVIEW
    assert review.approval_id == approval.id
    assert review.actor_agent_name == "Bolt"
    assert review.summary == "Merge needs a human"
    assert review.detail_json["action_type"] == "github.pr.merge"

    started_child = by_id[f"task:{child.id}:started"]
    assert started_child.kind is ActivityKind.STARTED
    assert started_child.task_title == "Implement the API"

    # Workspace-wide feed includes the stray task; agent filter matches targets too.
    everything = await service.list_activity(session, ws)
    assert f"task:{stray.id}:started" in {c.id for c in everything.items}
    for_builder = await service.list_activity(session, ws, agent_id=builder.id)
    assert f"msg:{delegation.id}" in {c.id for c in for_builder.items}
    assert f"task:{root.id}:started" not in {c.id for c in for_builder.items}

    only_reviews = await service.list_activity(
        session, ws, kinds={ActivityKind.NEEDS_REVIEW}, limit=1
    )
    assert [c.id for c in only_reviews.items] == [f"approval:{approval.id}"]
    assert only_reviews.next_before == at(13)

    page = await service.list_activity(session, ws, before=at(12), limit=2)
    assert [c.id for c in page.items] == [f"msg:{delegation.id}", f"task:{child.id}:started"]
    assert page.next_before == at(10)


async def test_attention_summary_counts(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    ws = admin_ctx.workspace_id
    waiting, waiting_turn = await start(session, admin_ctx, temporal, agent, text="Deploy it")
    session.add(
        AgentRun(
            workspace_id=ws,
            agent_id=agent.id,
            task_id=waiting_turn.task.id,
            status=RunStatus.WAITING_APPROVAL.value,
        )
    )
    session.add(
        Approval(
            workspace_id=ws,
            task_id=waiting_turn.task.id,
            requested_by_agent_id=agent.id,
            action_type="vercel.deploy",
            reason="Production deploy",
            status=ApprovalStatus.PENDING.value,
            requested_at=at(1),
        )
    )
    session.add(
        Approval(
            workspace_id=ws,
            task_id=waiting_turn.task.id,
            requested_by_agent_id=agent.id,
            action_type="vercel.deploy",
            reason="Old one",
            status=ApprovalStatus.APPROVED.value,
            requested_at=at(0),
        )
    )
    failed = Task(
        workspace_id=ws,
        title="Broken",
        state=TaskState.FAILED.value,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    old_failure = Task(
        workspace_id=ws,
        title="Ancient",
        state=TaskState.FAILED.value,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
        created_at=T0 - timedelta(days=30),
        updated_at=T0 - timedelta(days=30),
    )
    session.add_all([failed, old_failure])
    await session.commit()
    _calm, _ = await start(session, admin_ctx, temporal, agent, text="Just chatting")

    out = await service.attention(session, ws)
    assert [a.reason for a in out.pending_approvals] == ["Production deploy"]
    assert [t.id for t in out.failed_tasks] == [failed.id]
    assert [c.id for c in out.waiting_conversations] == [waiting.id]
    assert out.waiting_conversations[0].active_run_status == RunStatus.WAITING_APPROVAL.value
    assert out.counts.approvals == 1 and out.counts.failures == 1 and out.counts.total == 3


def _failed_task(ws: UUID, agent: Agent, title: str) -> Task:
    return Task(
        workspace_id=ws,
        title=title,
        state=TaskState.FAILED.value,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )


async def test_acknowledged_failures_leave_attention(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Dismissing a failure stamps the task, audits once, and drops it from
    the inbox; the task itself keeps its failed state."""
    ws = admin_ctx.workspace_id
    first = _failed_task(ws, agent, "Broken A")
    second = _failed_task(ws, agent, "Broken B")
    running = Task(
        workspace_id=ws,
        title="Still going",
        state=TaskState.RUNNING.value,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    session.add_all([first, second, running])
    await session.commit()

    before = await service.attention(session, ws)
    assert {t.id for t in before.failed_tasks} == {first.id, second.id}
    assert before.counts.failures == 2

    task = await tasks_service.acknowledge_task(
        session, admin_ctx, first.id, request_id=new_uuid7(), ip_hash="h"
    )
    assert task.state == TaskState.FAILED.value
    stamped = task.metadata_json[tasks_service.ATTENTION_ACKNOWLEDGED_KEY]
    assert isinstance(stamped, str) and stamped

    # Idempotent: same stamp, no second audit row.
    again = await tasks_service.acknowledge_task(
        session, admin_ctx, first.id, request_id=new_uuid7(), ip_hash="h"
    )
    assert again.metadata_json[tasks_service.ATTENTION_ACKNOWLEDGED_KEY] == stamped
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "task.acknowledged", AuditEvent.target_id == first.id
            )
        )
    )
    assert len(audits) == 1 and audits[0].actor_id == admin_ctx.user.id

    after = await service.attention(session, ws)
    assert [t.id for t in after.failed_tasks] == [second.id]
    assert after.counts.failures == 1 and after.counts.total == 1

    # Only failed tasks can be dismissed.
    with pytest.raises(HTTPException) as excinfo:
        await tasks_service.acknowledge_task(
            session, admin_ctx, running.id, request_id=new_uuid7(), ip_hash="h"
        )
    assert excinfo.value.status_code == 409


async def test_acknowledge_failures_in_bulk(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    ws = admin_ctx.workspace_id
    listed = [_failed_task(ws, agent, f"Broken {i}") for i in range(3)]
    stale = _failed_task(ws, agent, "Ancient")
    stale.created_at = T0 - timedelta(days=30)
    stale.updated_at = T0 - timedelta(days=30)
    session.add_all([*listed, stale])
    await session.commit()
    # One is already dismissed; the bulk call must not re-audit it.
    await tasks_service.acknowledge_task(
        session, admin_ctx, listed[0].id, request_id=new_uuid7(), ip_hash="h"
    )

    out = await service.acknowledge_failures(
        session, admin_ctx, request_id=new_uuid7(), ip_hash="h"
    )
    assert out.acknowledged == 2
    assert set(out.task_ids) == {listed[1].id, listed[2].id}
    audits = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "task.acknowledged"))
    )
    assert len(audits) == 3
    assert sum(1 for a in audits if a.metadata_json.get("bulk")) == 2
    after = await service.attention(session, ws)
    assert after.failed_tasks == [] and after.counts.failures == 0
    # The stale failure outside the window was never in the inbox, so it is untouched.
    await session.refresh(stale)
    assert not tasks_service.is_attention_acknowledged(stale)
    # A second bulk call is a no-op.
    assert (
        await service.acknowledge_failures(session, admin_ctx, request_id=new_uuid7(), ip_hash="h")
    ).acknowledged == 0


async def test_attention_lists_agent_reviews_in_progress(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Reviews an AI colleague is handling are listed separately (with the
    reviewer and the parked tool call) and never counted as needing a human."""
    ws = admin_ctx.workspace_id
    reviewer = Agent(workspace_id=ws, name="Ada", slug="ada", role_title="CTO")
    session.add(reviewer)
    task = Task(
        workspace_id=ws,
        title="Ship the thing",
        state=TaskState.RUNNING.value,
        assigned_agent_id=agent.id,
        correlation_id=new_uuid7(),
    )
    session.add(task)
    await session.flush()
    run = AgentRun(
        workspace_id=ws, agent_id=agent.id, task_id=task.id, status=RunStatus.RUNNING.value
    )
    session.add(run)
    await session.flush()
    call = ToolCall(
        workspace_id=ws,
        run_id=run.id,
        agent_id=agent.id,
        tool_name="github.pull_request.create",
        status=ToolCallStatus.PENDING_REVIEW.value,
    )
    session.add(call)
    await session.flush()
    by_agent = WorkReview(
        workspace_id=ws,
        task_id=task.id,
        run_id=run.id,
        tool_call_id=call.id,
        subject_agent_id=agent.id,
        trigger_key="agent-review",
        mode="pre_action",
        reviewer_type=ReviewerType.AGENT.value,
        reviewer_agent_id=reviewer.id,
        status=WorkReviewStatus.PENDING.value,
        requested_at=at(5),
    )
    by_human = WorkReview(
        workspace_id=ws,
        task_id=task.id,
        subject_agent_id=agent.id,
        trigger_key="human-review",
        mode="before_close",
        reviewer_type=ReviewerType.HUMAN.value,
        status=WorkReviewStatus.PENDING.value,
        requested_at=at(6),
    )
    decided = WorkReview(
        workspace_id=ws,
        task_id=task.id,
        subject_agent_id=agent.id,
        trigger_key="done-review",
        mode="pre_action",
        reviewer_type=ReviewerType.AGENT.value,
        reviewer_agent_id=reviewer.id,
        status=WorkReviewStatus.APPROVED.value,
        verdict="approve",
        requested_at=at(1),
    )
    session.add_all([by_agent, by_human, decided])
    await session.commit()

    out = await service.attention(session, ws)
    assert [r.id for r in out.pending_reviews] == [by_human.id]
    assert [r.id for r in out.reviews_in_progress] == [by_agent.id]
    in_progress = out.reviews_in_progress[0]
    assert in_progress.reviewer_agent_name == "Ada"
    assert in_progress.subject_agent_name == "Atlas"
    assert in_progress.task_title == "Ship the thing"
    assert in_progress.parked_tool_name == "github.pull_request.create"
    assert in_progress.parked_tool_call_status == ToolCallStatus.PENDING_REVIEW.value
    assert out.counts.reviews == 1
    assert out.counts.reviews_in_progress == 1
    assert out.counts.total == 1


async def test_delete_keeps_tasks_and_unlinks(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    conversation, result = await start(session, admin_ctx, temporal, agent)
    await service.delete_conversation(
        session, admin_ctx, conversation.id, request_id=new_uuid7(), ip_hash="h"
    )
    task = await session.get(Task, result.task.id)
    assert task is not None and task.conversation_id is None
    message = await session.get(Message, result.message.id)
    assert message is not None and message.conversation_id is None
    assert await session.get(Conversation, conversation.id) is None
    assert await session.get(User, admin_ctx.user.id) is not None


async def test_legacy_message_agent_opens_a_conversation(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    from jhin_api.tasks import service as tasks_service

    task = await tasks_service.message_agent(
        session,
        admin_ctx,
        temporal,  # type: ignore[arg-type]
        agent.id,
        text="Legacy hello",
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert task.metadata_json["origin"] == "message"
    conversation_id = UUID(task.metadata_json["conversation_id"])
    assert task.conversation_id == conversation_id
    conversation = await session.get(Conversation, conversation_id)
    assert conversation is not None and conversation.title == "Legacy hello"
    assert conversation.primary_agent_id == agent.id
    seed = await session.scalar(select(Message).where(Message.task_id == task.id))
    assert seed is not None and seed.conversation_id == conversation_id


async def test_failed_activity_card_explains_the_run_failure(
    session: AsyncSession, admin_ctx: WorkspaceContext, temporal: FakeTemporal, agent: Agent
) -> None:
    ws = admin_ctx.workspace_id
    _conversation, turn = await start(session, admin_ctx, temporal, agent, text="Say hi")
    task = turn.task
    task.state = TaskState.FAILED.value
    session.add(
        AgentRun(
            workspace_id=ws,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.FAILED.value,
            error_code="step_failed",
            error_message="openai: HTTP 429: You exceeded your current quota",
        )
    )
    await session.commit()

    feed = await service.list_activity(session, ws, kinds={ActivityKind.FAILED})
    [card] = [c for c in feed.items if c.task_id == task.id]
    assert card.summary.endswith("openai: HTTP 429: You exceeded your current quota")
    assert card.detail_json["error_message"].startswith("openai: HTTP 429")
