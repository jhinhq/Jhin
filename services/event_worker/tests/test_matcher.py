"""TriggerMatcher: matching, both dedupe layers, failure recording, audit."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_db.base import Base
from jhin_db.models import Agent, AuditEvent, Team, Trigger, TriggerInvocation, Workspace
from jhin_domain import AgentStatus, new_uuid7
from jhin_event_worker.matcher import TriggerMatcher
from jhin_events.envelope import EventEnvelope, EventSource
from jhin_observability import noop_metrics, noop_tracer

TODO_FILTER = {
    "all": [
        {"path": "data.team.key", "op": "eq", "value": "ENG"},
        {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
    ]
}


class FakeTemporal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_already_started = False
        self.raise_error: Exception | None = None

    async def start_workflow(self, name: str, params: Any, *, id: str, task_queue: str) -> None:
        if self.raise_error is not None:
            raise self.raise_error
        if self.raise_already_started or any(call["id"] == id for call in self.calls):
            # Temporal owns duplicate-start idempotency for a deterministic id.
            raise WorkflowAlreadyStartedError(id, name)
        self.calls.append({"name": name, "params": params, "id": id, "task_queue": task_queue})


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def ids(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    async with session_factory() as session:
        workspace = Workspace(name="W", slug=f"w-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id, name="SWE", slug="swe", status=AgentStatus.ACTIVE.value
        )
        session.add(agent)
        await session.flush()
        connection_id = new_uuid7()
        trigger = Trigger(
            workspace_id=workspace.id,
            name="Pick up new engineering tickets",
            enabled=True,
            connection_id=None,  # FK-free in tests; matcher only compares ids
            event_type="connector.linear.issue.updated",
            filter_json=TODO_FILTER,
            target_agent_id=agent.id,
            dedupe_window_seconds=300,
        )
        session.add(trigger)
        await session.commit()
        return {
            "workspace": workspace.id,
            "agent": agent.id,
            "trigger": trigger.id,
            "connection": connection_id,
        }


def issue_event(
    workspace_id: UUID,
    *,
    state: str = "Todo",
    team: str = "ENG",
    changed: bool = True,
    event_id: UUID | None = None,
) -> EventEnvelope:
    data: dict[str, Any] = {
        "external_id": "ENG-142",
        "title": "Fix the failing test",
        "description": "Make scripts/run_tests.sh pass.",
        "url": "https://linear.example/issue/ENG-142",
        "team": {"id": "team-eng", "key": team, "name": "Engineering"},
        "state": {"id": f"state-{state.lower()}", "name": state, "type": "unstarted"},
        "changed_from": {"state": {"id": "state-backlog"}} if changed else {},
    }
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type="connector.linear.issue.updated",
        workspace_id=str(workspace_id),
        source=EventSource(type="linear", connection_id=None),
        data=data,
    )


def make_matcher(
    session_factory: async_sessionmaker[AsyncSession], temporal: FakeTemporal
) -> TriggerMatcher:
    return TriggerMatcher(
        session_factory,
        cast(Any, temporal),
        metrics=noop_metrics(),
        tracer=noop_tracer(),
        cache_ttl_seconds=0.0,
    )


async def invocations(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[TriggerInvocation]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(TriggerInvocation).order_by(TriggerInvocation.created_at)
        )
        return list(rows)


async def test_match_starts_workflow_and_records_invocation(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))

    assert len(temporal.calls) == 1
    call = temporal.calls[0]
    assert call["name"] == "TriggeredTaskWorkflow"
    assert call["id"].startswith("triggered-task-")
    assert call["params"].external_id == "ENG-142"
    assert call["params"].title == "[ENG-142] Fix the failing test"
    assert call["params"].agent_id == str(ids["agent"])

    rows = await invocations(session_factory)
    assert len(rows) == 1
    assert rows[0].status == "started"
    assert rows[0].workflow_id == call["id"]

    async with session_factory() as session:
        audits = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.action == "trigger.invoked"))
        )
    assert len(audits) == 1
    assert audits[0].metadata_json["status"] == "started"


async def test_engineering_template_selects_engineering_ticket_workflow(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    """Phase 8 (plan 8.4): trigger.workflow_definition selects the built-in
    template; the config rides along and malformed cycles fall back."""
    qa_id = str(new_uuid7())
    async with session_factory() as session:
        trigger = await session.get(Trigger, ids["trigger"])
        assert trigger is not None
        trigger.workflow_definition = {
            "template": "engineering_ticket",
            "qa_agent_id": qa_id,
            "manager_review": True,
            "max_retest_cycles": "not-a-number",
        }
        await session.commit()

    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))

    assert len(temporal.calls) == 1
    call = temporal.calls[0]
    assert call["name"] == "EngineeringTicketWorkflow"
    params = call["params"]
    assert params.base.external_id == "ENG-142"
    assert params.qa_agent_id == qa_id
    assert params.manager_review is True
    assert params.max_retest_cycles == 3  # malformed value falls back


async def test_unknown_template_falls_back_to_plain_workflow(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with session_factory() as session:
        trigger = await session.get(Trigger, ids["trigger"])
        assert trigger is not None
        trigger.workflow_definition = {"template": "does_not_exist"}
        await session.commit()

    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))

    assert len(temporal.calls) == 1
    assert temporal.calls[0]["name"] == "TriggeredTaskWorkflow"


async def test_semantically_identical_event_is_suppressed(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))
    # Same transition, brand-new event id (fresh delivery) — must not start
    # a second workflow (plan 48.6).
    await matcher.handle_event(issue_event(ids["workspace"]))

    assert len(temporal.calls) == 1
    rows = await invocations(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate"]
    assert rows[0].idempotency_key == rows[1].idempotency_key


async def test_vercel_canonical_redelivery_starts_at_most_one_task(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with session_factory() as session:
        trigger = await session.get(Trigger, ids["trigger"])
        assert trigger is not None
        trigger.event_type = "connector.vercel.deployment.ready"
        trigger.filter_json = {}
        await session.commit()

    event = EventEnvelope(
        event_id=UUID("aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"),
        event_type="connector.vercel.deployment.ready",
        workspace_id=str(ids["workspace"]),
        source=EventSource(type="vercel", connection_id=ids["connection"]),
        data={
            "deployment_id": "dpl_123",
            "project_id": "prj_123",
            "project_name": "storefront",
            "url": "storefront-abc.vercel.app",
            "target": "preview",
            "state": "READY",
            "created_at": 1_700_000_000_000,
            "git_ref": "agent/fix",
            "git_sha": "abc123",
        },
    )
    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)

    await matcher.handle_event(event)
    await matcher.handle_event(event)

    assert len(temporal.calls) == 1
    rows = await invocations(session_factory)
    assert [row.status for row in rows] == ["started", "duplicate"]
    assert rows[0].idempotency_key == rows[1].idempotency_key
    assert rows[0].event_id == rows[1].event_id == event.event_id


async def test_no_transition_and_filter_miss_do_not_invoke(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    # Title edit: state did not change.
    await matcher.handle_event(issue_event(ids["workspace"], changed=False))
    # Transition on another team.
    await matcher.handle_event(issue_event(ids["workspace"], team="OPS"))
    # Different event type entirely.
    await matcher.handle_event(
        EventEnvelope(
            event_type="connector.linear.comment.created",
            workspace_id=str(ids["workspace"]),
            source=EventSource(type="linear"),
            data={"external_id": "c-1"},
        )
    )
    # Non-connector events are ignored outright.
    await matcher.handle_event(
        EventEnvelope(
            event_type="task.completed",
            workspace_id=str(ids["workspace"]),
            source=EventSource(type="api"),
            data={},
        )
    )
    assert temporal.calls == []
    assert await invocations(session_factory) == []


async def test_workflow_already_started_keeps_invocation_started(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    temporal = FakeTemporal()
    temporal.raise_already_started = True
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))
    rows = await invocations(session_factory)
    assert [row.status for row in rows] == ["started"]


async def test_temporal_failure_marks_failed_and_raises(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    temporal = FakeTemporal()
    temporal.raise_error = RuntimeError("temporal down")
    matcher = make_matcher(session_factory, temporal)
    with pytest.raises(RuntimeError):
        await matcher.handle_event(issue_event(ids["workspace"]))
    rows = await invocations(session_factory)
    assert [row.status for row in rows] == ["failed"]
    # Only the closed safe code is persisted; never the provider message.
    assert rows[0].error == "upstream_unavailable"

    # Redelivery after the failure starts cleanly: the partial unique index
    # only guards *started* rows.
    temporal.raise_error = None
    await matcher.handle_event(issue_event(ids["workspace"]))
    rows = await invocations(session_factory)
    assert sorted(row.status for row in rows) == ["failed", "started"]
    assert len(temporal.calls) == 1


async def test_missing_agent_marks_invocation_failed(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with session_factory() as session:
        agent = await session.get(Agent, ids["agent"])
        assert agent is not None
        agent.status = AgentStatus.DISABLED.value
        await session.commit()

    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))
    assert temporal.calls == []
    rows = await invocations(session_factory)
    assert [row.status for row in rows] == ["failed"]
    assert rows[0].error == "invalid_request"


async def test_team_target_resolves_to_active_member(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with session_factory() as session:
        team = Team(workspace_id=ids["workspace"], name="Platform")
        session.add(team)
        await session.flush()
        member = Agent(
            workspace_id=ids["workspace"],
            name="Platform SWE",
            slug="platform-swe",
            team_id=team.id,
            status=AgentStatus.ACTIVE.value,
        )
        session.add(member)
        await session.flush()
        trigger = await session.get(Trigger, ids["trigger"])
        assert trigger is not None
        trigger.target_agent_id = None
        trigger.target_team_id = team.id
        await session.commit()
        member_id = member.id

    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))
    assert len(temporal.calls) == 1
    assert temporal.calls[0]["params"].agent_id == str(member_id)


async def test_disabled_trigger_is_not_matched(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> None:
    async with session_factory() as session:
        trigger = await session.get(Trigger, ids["trigger"])
        assert trigger is not None
        trigger.enabled = False
        await session.commit()

    temporal = FakeTemporal()
    matcher = make_matcher(session_factory, temporal)
    await matcher.handle_event(issue_event(ids["workspace"]))
    assert temporal.calls == []
