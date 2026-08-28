"""Coordination activity helpers: lifting accepted requests into the
workflow contract, roster/manager prompt context, and the finalize activity
against SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_agent_worker.coordination_activities import (
    CoordinationActivities,
    manager_context,
    organization_context,
    work_request_start_from_output,
)
from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Message, Task, WorkRequest, Workspace
from jhin_domain import (
    MessageType,
    MessageVisibility,
    TaskState,
    WorkRequestStatus,
    new_uuid7,
)
from jhin_workflows.agent_task.shared import (
    WORK_REQUEST_SIDE_REQUESTER,
    WORK_REQUEST_SIDE_RESPONDER,
)
from jhin_workflows.work_request_task import (
    FinalizeWorkRequestInput,
    NoteWorkRequestUnansweredInput,
)

_REQUEST = "organization.request_work"
_RESPOND = "organization.respond_work_request"


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeResources:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory


def test_work_request_start_lifting() -> None:
    accepted = {"work_request_id": "r", "created_task_id": "t", "status": "accepted"}
    assert work_request_start_from_output(None, tool_name=_RESPOND) is None
    assert (
        work_request_start_from_output(
            {"work_request_id": "r", "created_task_id": None}, tool_name=_RESPOND
        )
        is None
    )
    start = work_request_start_from_output(accepted, tool_name=_RESPOND)
    assert start is not None and start.task_id == "t" and start.work_request_id == "r"
    # Any other tool creating a task-shaped output starts nothing at all.
    assert work_request_start_from_output(accepted, tool_name="organization.delegate_task") is None


def test_the_lifted_side_comes_from_the_tool_that_ran() -> None:
    """Which side of the ask the running agent is on decides whether its
    workflow may park on the answer. Getting it wrong on the responder's
    step parks a colleague on the task it just accepted for itself, so the
    role is read off the tool — the requester's tool is its own, and the
    responder's is limited by its validator to the request's target."""
    accepted = {"work_request_id": "r", "created_task_id": "t", "agent_id": "a"}
    asked = work_request_start_from_output(accepted, tool_name=_REQUEST)
    answered = work_request_start_from_output(accepted, tool_name=_RESPOND)
    assert asked is not None and asked.side == WORK_REQUEST_SIDE_REQUESTER
    assert answered is not None and answered.side == WORK_REQUEST_SIDE_RESPONDER


async def test_context_blocks_and_finalize_activity(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        cto = Agent(workspace_id=workspace.id, name="CTO", slug="cto")
        session.add(cto)
        await session.flush()
        swe = Agent(workspace_id=workspace.id, name="SWE", slug="swe", manager_agent_id=cto.id)
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        session.add_all([swe, writer])
        await session.flush()
        requester_task = Task(
            workspace_id=workspace.id,
            title="Parent",
            state=TaskState.RUNNING.value,
            assigned_agent_id=swe.id,
            correlation_id=new_uuid7(),
        )
        session.add(requester_task)
        await session.flush()
        created = Task(
            workspace_id=workspace.id,
            title="Docs",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=writer.id,
            correlation_id=new_uuid7(),
            metadata_json={"reported_result": {"summary": "Docs done", "status": "completed"}},
        )
        session.add(created)
        await session.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=swe.id,
            requester_task_id=requester_task.id,
            target_agent_id=writer.id,
            title="Docs",
            idempotency_key="k",
            status=WorkRequestStatus.ACCEPTED.value,
            created_task_id=created.id,
            metadata_json={"requester_agent_name": "SWE", "target_agent_name": "Writer"},
        )
        session.add(request)
        await session.commit()

        roster = await organization_context(session, workspace.id, swe.id)
        assert "Your manager:" in roster and "CTO" in roster
        assert roster.startswith("Your colleagues.")
        # No agent-id-consuming grant, so ids stay out of the prompt.
        assert "agent id" not in roster and str(cto.id) not in roster
        assert await manager_context(session, workspace.id, writer.id) == ""
        manager_block = await manager_context(session, workspace.id, cto.id)
        assert "SWE" in manager_block
        workspace_id, request_id, task_id = workspace.id, request.id, created.id

    activities = CoordinationActivities(FakeResources(maker))  # type: ignore[arg-type]
    params = FinalizeWorkRequestInput(
        workspace_id=str(workspace_id),
        work_request_id=str(request_id),
        task_id=str(task_id),
        run_status="completed",
    )
    assert await activities.finalize_work_request_activity(params) == "completed"
    assert await activities.finalize_work_request_activity(params) == "completed"
    async with maker() as session:
        row: Any = await session.get(WorkRequest, request_id)
        assert row.status == WorkRequestStatus.COMPLETED.value


async def test_finalize_starts_memory_maintenance_for_the_requester(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        swe = Agent(workspace_id=workspace.id, name="SWE", slug="swe")
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        session.add_all([swe, writer])
        await session.flush()
        requester_task = Task(
            workspace_id=workspace.id,
            title="Parent",
            state=TaskState.RUNNING.value,
            assigned_agent_id=swe.id,
            correlation_id=new_uuid7(),
        )
        session.add(requester_task)
        await session.flush()
        created = Task(
            workspace_id=workspace.id,
            title="Docs",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=writer.id,
            correlation_id=new_uuid7(),
            metadata_json={"reported_result": {"summary": "Docs done", "status": "completed"}},
        )
        session.add(created)
        await session.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=swe.id,
            requester_task_id=requester_task.id,
            target_agent_id=writer.id,
            title="Docs",
            idempotency_key="k2",
            status=WorkRequestStatus.ACCEPTED.value,
            created_task_id=created.id,
            metadata_json={"requester_agent_name": "SWE", "target_agent_name": "Writer"},
        )
        session.add(request)
        await session.commit()
        workspace_id, request_id, task_id = workspace.id, request.id, created.id
        requester_id, requester_task_uuid = swe.id, requester_task.id

    calls: list[Any] = []

    async def fake_start(client: Any, params: Any, **kwargs: Any) -> tuple[str, None]:
        calls.append(params)
        return "started", None

    monkeypatch.setattr(
        "jhin_agent_worker.coordination_activities.start_memory_maintenance", fake_start
    )
    activities = CoordinationActivities(
        FakeResources(maker),  # type: ignore[arg-type]
        temporal_client=object(),  # type: ignore[arg-type]
    )
    params = FinalizeWorkRequestInput(
        workspace_id=str(workspace_id),
        work_request_id=str(request_id),
        task_id=str(task_id),
        run_status="completed",
    )
    assert await activities.finalize_work_request_activity(params) == "completed"
    assert len(calls) == 1
    start = calls[0]
    assert start.source_kind == "message"
    assert start.agent_id == str(requester_id)  # the requester learns
    assert start.task_id == str(requester_task_uuid)
    async with maker() as session:
        row: Any = await session.get(WorkRequest, request_id)
        assert start.source_id == row.metadata_json["result_message_id"]


async def test_roster_prints_ids_only_when_the_agent_can_use_them(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The activity reads the agent's allow-grants purely to shape the
    block: an agent that can ask a colleague for work needs the ids its
    tool takes; one that cannot is spared the noise."""
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        cto = Agent(
            workspace_id=workspace.id,
            name="CTO",
            slug="cto",
            role_title="Chief Technology Officer",
        )
        session.add(cto)
        await session.flush()
        bisby = Agent(
            workspace_id=workspace.id,
            name="Bisby",
            slug="bisby",
            role_title="Senior Software Engineer",
            manager_agent_id=cto.id,
        )
        session.add(bisby)
        await session.flush()

        session.add_all(
            [
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=bisby.id,
                    capability=capability,
                    scope_json={},
                    effect=effect,
                )
                for capability, effect in (
                    ("organization.work.request", "allow"),
                    ("organization.directory.read", "allow"),
                    ("organization.delegate", "deny"),
                )
            ]
        )
        await session.flush()

        roster = await organization_context(session, workspace.id, bisby.id)
        assert f"[agent id: {cto.id}]" in roster
        assert "Never write an id in a message to a person" in roster
        assert "organization.directory.search before answering" in roster
        # Deny rows are not grants: they never turn presentation on.
        no_grants = await organization_context(session, workspace.id, cto.id)
        assert "agent id" not in no_grants


async def test_an_unanswered_request_leaves_a_mark_the_requester_can_read(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The requester holds its turn open for the answer. When that wait
    elapses the run carries on, so the next model step has to find the truth
    on the task — otherwise it repeats the promise the wait was added to
    kill. Written once, and never over a request that has since finished."""
    async with maker() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        swe = Agent(workspace_id=workspace.id, name="SWE", slug="swe")
        cto = Agent(workspace_id=workspace.id, name="CTO", slug="cto")
        session.add_all([swe, cto])
        await session.flush()
        requester_task = Task(
            workspace_id=workspace.id,
            title="Parent",
            state=TaskState.RUNNING.value,
            assigned_agent_id=swe.id,
            correlation_id=new_uuid7(),
        )
        session.add(requester_task)
        await session.flush()
        request = WorkRequest(
            workspace_id=workspace.id,
            requester_agent_id=swe.id,
            requester_task_id=requester_task.id,
            target_agent_id=cto.id,
            title="What are you working on?",
            idempotency_key="k3",
            status=WorkRequestStatus.ACCEPTED.value,
            metadata_json={"requester_agent_name": "SWE", "target_agent_name": "CTO"},
        )
        session.add(request)
        await session.commit()
        workspace_id, request_id, requester_task_id = workspace.id, request.id, requester_task.id

    activities = CoordinationActivities(FakeResources(maker))  # type: ignore[arg-type]
    params = NoteWorkRequestUnansweredInput(
        workspace_id=str(workspace_id), work_request_id=str(request_id)
    )
    assert await activities.note_work_request_unanswered_activity(params) == "noted"
    # An activity retry after a committed write says nothing twice.
    assert await activities.note_work_request_unanswered_activity(params) == "noted"

    async with maker() as session:
        notes = list(
            await session.scalars(
                select(Message).where(
                    Message.task_id == requester_task_id,
                    Message.message_type == MessageType.STATUS.value,
                )
            )
        )
        assert len(notes) == 1
        assert "CTO has not answered yet" in notes[0].content_json["summary"]
        assert notes[0].content_json["waiting"] is True
        # Visible, so it reaches the requester's next prompt at all.
        assert notes[0].visibility == MessageVisibility.VISIBLE.value
        row: Any = await session.get(WorkRequest, request_id)
        row.status = WorkRequestStatus.COMPLETED.value
        row.metadata_json = {
            key: value
            for key, value in row.metadata_json.items()
            if key != "waiting_note_message_id"
        }
        await session.commit()

    # The colleague answered while the timer was firing: the real result is
    # already posted, and "still waiting" after it would be a lie.
    assert await activities.note_work_request_unanswered_activity(params) == "terminal"
    async with maker() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(Message).where(
                            Message.task_id == requester_task_id,
                            Message.message_type == MessageType.STATUS.value,
                        )
                    )
                )
            )
            == 1
        )
