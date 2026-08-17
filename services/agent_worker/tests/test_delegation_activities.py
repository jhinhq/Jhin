"""Delegation summarize/deliver activities + concurrency admission (plan
7.6, 29, 30) against in-memory SQLite with a stub event publisher.

The summarizer is the plan-7.6 boundary: parents receive the standardized
summary built from the child's *reported* result (deterministic evidence),
never the child's transcript. Review verdicts are deny-by-default: only an
explicitly reported "pass" passes.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.testing import ActivityEnvironment

from jhin_agent_worker.activities import AgentActivities
from jhin_db.base import Base
from jhin_db.models import Agent, AgentRun, AuditEvent, Message, RunEvent, Task, Workspace
from jhin_domain import MessageType, RunStatus, TaskState, new_uuid7
from jhin_workflows.agent_task import AgentTaskInput
from jhin_workflows.delegated_task import (
    DelegationSummary,
    DeliverDelegationResultInput,
    SummarizeDelegationInput,
)


class StubPublisher:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.events.append(envelope)


class StubResources:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory
        self.publisher = StubPublisher()
        self.crypto = None


class World:
    workspace: Workspace
    cto: Agent
    swe: Agent
    parent_task: Task
    child_task: Task
    activities: AgentActivities
    publisher: StubPublisher
    session_factory: Any


@pytest.fixture
async def world() -> Any:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    fixture = World()
    fixture.session_factory = maker
    resources = StubResources(maker)
    fixture.publisher = resources.publisher
    fixture.activities = AgentActivities(resources)  # type: ignore[arg-type]

    async with maker() as session:
        fixture.workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(fixture.workspace)
        await session.flush()
        ws = fixture.workspace.id
        fixture.cto = Agent(workspace_id=ws, name="CTO", slug="cto")
        session.add(fixture.cto)
        await session.flush()
        fixture.swe = Agent(
            workspace_id=ws, name="SWE", slug="swe", manager_agent_id=fixture.cto.id
        )
        session.add(fixture.swe)
        await session.flush()
        fixture.parent_task = Task(
            workspace_id=ws,
            title="Parent",
            state=TaskState.RUNNING.value,
            assigned_agent_id=fixture.cto.id,
            correlation_id=new_uuid7(),
        )
        session.add(fixture.parent_task)
        await session.flush()
        fixture.child_task = Task(
            workspace_id=ws,
            title="Child",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=fixture.swe.id,
            parent_task_id=fixture.parent_task.id,
            correlation_id=fixture.parent_task.correlation_id,
        )
        session.add(fixture.child_task)
        await session.commit()

    yield fixture
    await engine.dispose()


def summarize_input(world: World, **overrides: Any) -> SummarizeDelegationInput:
    values: dict[str, Any] = {
        "workspace_id": str(world.workspace.id),
        "parent_task_id": str(world.parent_task.id),
        "child_task_id": str(world.child_task.id),
        "agent_id": str(world.swe.id),
        "delegating_agent_id": str(world.cto.id),
        "kind": "delegation",
        "run_status": "completed",
        "parent_run_id": "",
        "blocking": False,
    }
    values.update(overrides)
    return SummarizeDelegationInput(**values)


async def set_reported(world: World, reported: dict[str, Any]) -> None:
    async with world.session_factory() as session:
        task = await session.get(Task, world.child_task.id)
        task.metadata_json = {**task.metadata_json, "reported_result": reported}
        await session.commit()


# --- summarize_delegation ---


async def test_summary_from_reported_result_and_parent_message(world: World) -> None:
    await set_reported(
        world,
        {
            "summary": "Implemented and opened PR #7.",
            "status": "completed",
            "artifacts": [{"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}],
            "risks": ["touches auth"],
            "recommended_next_action": "delegate_to_qa",
        },
    )
    summary = await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity, summarize_input(world)
    )
    assert summary.reported is True
    assert summary.status == "completed"
    assert summary.summary == "Implemented and opened PR #7."
    assert summary.artifacts == [
        {"type": "github_pull_request", "id": "7", "url_ref": "http://gh/7"}
    ]
    assert summary.risks == ["touches auth"]
    assert summary.recommended_next_action == "delegate_to_qa"
    assert summary.verdict == ""

    async with world.session_factory() as session:
        message = await session.scalar(
            select(Message).where(Message.message_type == MessageType.RESULT.value)
        )
        assert message is not None
        assert message.task_id == world.parent_task.id  # lands on the parent
        assert message.sender_id == world.swe.id
        assert message.recipient_id == world.cto.id
        assert message.content_json["summary"] == "Implemented and opened PR #7."
        assert message.content_json["child_task_id"] == str(world.child_task.id)
        assert "delivered" not in message.content_json  # non-blocking: model-visible
        actions = list(await session.scalars(select(AuditEvent.action)))
        assert "task.delegation_completed" in actions
    assert any(e.event_type == "task.delegation_completed" for e in world.publisher.events)


async def test_review_verdict_pass_only_when_explicitly_reported(world: World) -> None:
    await set_reported(world, {"summary": "All tests green.", "status": "pass"})
    summary = await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity,
        summarize_input(world, kind="review_request"),
    )
    assert summary.verdict == "pass"
    async with world.session_factory() as session:
        message = await session.scalar(
            select(Message).where(Message.message_type == MessageType.REVIEW_RESULT.value)
        )
        assert message is not None
        assert message.content_json["verdict"] == "pass"
        actions = list(await session.scalars(select(AuditEvent.action)))
        assert "task.review_completed" in actions


async def test_review_verdict_fails_without_explicit_pass(world: World) -> None:
    # A completed run that never reported → fail (deny-by-default reviews).
    summary = await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity,
        summarize_input(world, kind="review_request"),
    )
    assert summary.verdict == "fail"
    assert summary.reported is False

    await set_reported(world, {"summary": "Tests fail on the branch.", "status": "fail"})
    summary = await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity,
        summarize_input(world, kind="review_request"),
    )
    assert summary.verdict == "fail"
    assert summary.summary == "Tests fail on the branch."


async def test_unreported_child_falls_back_to_last_visible_text(world: World) -> None:
    async with world.session_factory() as session:
        session.add(
            Message(
                workspace_id=world.workspace.id,
                task_id=world.child_task.id,
                sender_type="agent",
                sender_id=world.swe.id,
                recipient_type="task",
                recipient_id=world.child_task.id,
                message_type="text",
                content_json={"text": "Done, PR is open."},
            )
        )
        await session.commit()
    summary = await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity, summarize_input(world)
    )
    assert summary.reported is False
    assert summary.summary == "Done, PR is open."
    assert summary.status == "completed"  # falls back to the run status


async def test_blocking_summary_message_is_marked_delivered(world: World) -> None:
    await set_reported(world, {"summary": "done", "status": "completed"})
    await ActivityEnvironment().run(
        world.activities.summarize_delegation_activity,
        summarize_input(world, blocking=True),
    )
    async with world.session_factory() as session:
        message = await session.scalar(
            select(Message).where(Message.message_type == MessageType.RESULT.value)
        )
        assert message is not None
        # Marked so history rebuilding skips it: the deliver activity stitches
        # the same summary in as the tool observation.
        assert message.content_json["delivered"] == "observation"


# --- deliver_delegation_result ---


async def test_deliver_writes_observation_and_resumes_run(world: World) -> None:
    async with world.session_factory() as session:
        run = AgentRun(
            workspace_id=world.workspace.id,
            agent_id=world.cto.id,
            task_id=world.parent_task.id,
            status=RunStatus.WAITING_DELEGATION.value,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    summary = DelegationSummary(
        task_id=str(world.child_task.id),
        status="pass",
        summary="QA passed.",
        verdict="pass",
        reported=True,
    )
    await ActivityEnvironment().run(
        world.activities.deliver_delegation_result_activity,
        DeliverDelegationResultInput(
            workspace_id=str(world.workspace.id),
            task_id=str(world.parent_task.id),
            run_id=str(run_id),
            agent_id=str(world.cto.id),
            child_task_id=str(world.child_task.id),
            provider_call_id="call-9",
            kind="review_request",
            summary=summary,
        ),
    )
    async with world.session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == RunStatus.RUNNING.value
        observation = await session.scalar(
            select(Message).where(Message.message_type == "tool_result")
        )
        assert observation is not None
        assert observation.content_json["tool_call_id"] == "call-9"
        assert observation.content_json["tool_name"] == "organization.delegate_task"
        payload = json.loads(observation.content_json["result"])
        assert payload["verdict"] == "pass"
        assert payload["summary"] == "QA passed."
        event = await session.scalar(
            select(RunEvent).where(RunEvent.event_type == "delegation.result")
        )
        assert event is not None
        assert event.payload_json["verdict"] == "pass"
    assert any(e.event_type == "agent.run.resumed" for e in world.publisher.events)


# --- concurrency admission (plan 30) ---


def snapshot_input(world: World, task: Task) -> AgentTaskInput:
    return AgentTaskInput(
        workspace_id=str(world.workspace.id),
        task_id=str(task.id),
        agent_id=str(world.swe.id),
    )


async def add_active_run(world: World, agent_id: UUID, status: str = "running") -> None:
    async with world.session_factory() as session:
        session.add(
            AgentRun(
                workspace_id=world.workspace.id,
                agent_id=agent_id,
                task_id=world.parent_task.id,
                status=status,
            )
        )
        await session.commit()


async def test_agent_limit_queues_second_run(world: World) -> None:
    await add_active_run(world, world.swe.id)  # SWE default limit is 1
    async with world.session_factory() as session:
        task = Task(
            workspace_id=world.workspace.id,
            title="Second ticket",
            state=TaskState.RUNNING.value,
            assigned_agent_id=world.swe.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.commit()

    result = await ActivityEnvironment().run(
        world.activities.resolve_snapshot_activity, snapshot_input(world, task)
    )
    assert result.queued is True
    assert result.queue_reason == "agent_concurrency"
    assert result.run_id == ""

    async with world.session_factory() as session:
        refreshed = await session.get(Task, task.id)
        assert refreshed is not None
        assert refreshed.state == TaskState.QUEUED.value
        assert refreshed.metadata_json["queue"]["reason"] == "agent_concurrency"
    assert any(e.event_type == "task.queued" for e in world.publisher.events)


async def test_parked_runs_hold_their_slot(world: World) -> None:
    # A run waiting on a delegation still owns its working state (plan 30).
    await add_active_run(world, world.swe.id, status=RunStatus.WAITING_DELEGATION.value)
    result = await ActivityEnvironment().run(
        world.activities.resolve_snapshot_activity, snapshot_input(world, world.parent_task)
    )
    assert result.queued is True


async def test_workspace_limit_queues_across_agents(world: World) -> None:
    async with world.session_factory() as session:
        workspace = await session.get(Workspace, world.workspace.id)
        workspace.settings_json = {"concurrency": {"max_concurrent_runs": 1}}
        await session.commit()
    await add_active_run(world, world.cto.id)  # a *different* agent holds the slot

    result = await ActivityEnvironment().run(
        world.activities.resolve_snapshot_activity, snapshot_input(world, world.parent_task)
    )
    assert result.queued is True
    assert result.queue_reason == "workspace_concurrency"


async def test_completed_runs_do_not_hold_slots(world: World) -> None:
    await add_active_run(world, world.swe.id, status=RunStatus.COMPLETED.value)
    # No active run → admission proceeds past the queue check into snapshot
    # resolution, which fails here (no model profile seeded) — proving the
    # slot was granted.
    with pytest.raises(Exception, match=r"model profile|snapshot|default"):
        await ActivityEnvironment().run(
            world.activities.resolve_snapshot_activity, snapshot_input(world, world.parent_task)
        )
