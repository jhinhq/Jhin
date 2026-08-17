"""Trigger service logic: CRUD validation, audited writes, dry-run testing."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.triggers import service
from jhin_api.triggers.schemas import TriggerCreate, TriggerUpdate
from jhin_db.models import Agent, AuditEvent, TriggerInvocation
from jhin_domain import new_uuid7

TODO_FILTER: dict[str, Any] = {
    "all": [
        {"path": "data.team.key", "op": "eq", "value": "ENG"},
        {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
    ]
}


@pytest.fixture
async def agent_id(session: AsyncSession, admin_ctx: WorkspaceContext) -> UUID:
    agent = Agent(workspace_id=admin_ctx.workspace_id, name="SWE", slug="swe")
    session.add(agent)
    await session.flush()
    return agent.id


def creation_payload(agent_id: UUID, **overrides: Any) -> TriggerCreate:
    values: dict[str, Any] = {
        "name": "Pick up new engineering tickets",
        "event_type": "connector.linear.issue.updated",
        "filter": TODO_FILTER,
        "target_agent_id": agent_id,
    }
    values.update(overrides)
    return TriggerCreate(**values)


def sample_event(*, state: str = "Todo", team: str = "ENG", changed: bool = True) -> dict[str, Any]:
    return {
        "event_type": "connector.linear.issue.updated",
        "data": {
            "external_id": "ENG-142",
            "team": {"key": team},
            "state": {"id": f"state-{state.lower()}", "name": state},
            "changed_from": {"state": {"id": "state-backlog"}} if changed else {},
        },
    }


async def test_create_and_list_with_audit(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    trigger = await service.create_trigger(
        session, admin_ctx, creation_payload(agent_id), request_id=new_uuid7(), ip_hash="h"
    )
    assert trigger.enabled is True
    assert trigger.filter_json == TODO_FILTER

    triggers = await service.list_triggers(session, admin_ctx.workspace_id)
    assert [t.id for t in triggers] == [trigger.id]

    audit_row = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "trigger.created")
    )
    assert audit_row is not None and audit_row.target_id == trigger.id


async def test_create_rejects_bad_filter_and_missing_target(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    bad_filter = creation_payload(
        agent_id, filter={"all": [{"path": "a", "op": "regex", "value": ".*"}]}
    )
    with pytest.raises(HTTPException) as excinfo:
        await service.create_trigger(
            session, admin_ctx, bad_filter, request_id=new_uuid7(), ip_hash="h"
        )
    assert "Invalid filter" in str(excinfo.value.detail)

    with pytest.raises(HTTPException) as excinfo:
        await service.create_trigger(
            session,
            admin_ctx,
            creation_payload(agent_id, target_agent_id=None),
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert "target_agent_id or target_team_id" in str(excinfo.value.detail)

    with pytest.raises(HTTPException):
        await service.create_trigger(
            session,
            admin_ctx,
            creation_payload(agent_id, event_type=None),
            request_id=new_uuid7(),
            ip_hash="h",
        )


async def test_create_rejects_foreign_references(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await service.create_trigger(
            session,
            admin_ctx,
            creation_payload(agent_id, target_agent_id=new_uuid7()),
            request_id=new_uuid7(),
            ip_hash="h",
        )
    assert "target_agent_id" in str(excinfo.value.detail)


async def test_update_enable_disable_delete(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    trigger = await service.create_trigger(
        session, admin_ctx, creation_payload(agent_id), request_id=new_uuid7(), ip_hash="h"
    )
    updated = await service.update_trigger(
        session,
        admin_ctx,
        trigger.id,
        TriggerUpdate(name="Renamed", action_config={"comment_back": True}),
        request_id=new_uuid7(),
        ip_hash="h",
    )
    assert updated.name == "Renamed"
    assert updated.action_config_json == {"comment_back": True}

    disabled = await service.set_enabled(
        session, admin_ctx, trigger.id, enabled=False, request_id=new_uuid7(), ip_hash="h"
    )
    assert disabled.enabled is False

    await service.delete_trigger(
        session, admin_ctx, trigger.id, request_id=new_uuid7(), ip_hash="h"
    )
    assert await service.list_triggers(session, admin_ctx.workspace_id) == []
    actions = [
        row.action
        for row in await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
    ]
    assert actions.count("trigger.updated") == 1
    assert actions.count("trigger.disabled") == 1
    assert actions.count("trigger.deleted") == 1


async def test_update_rejects_invalid_filter(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    trigger = await service.create_trigger(
        session, admin_ctx, creation_payload(agent_id), request_id=new_uuid7(), ip_hash="h"
    )
    with pytest.raises(HTTPException):
        await service.update_trigger(
            session,
            admin_ctx,
            trigger.id,
            TriggerUpdate(filter={"any": "nope"}),
            request_id=new_uuid7(),
            ip_hash="h",
        )


async def test_dry_run_explains_each_condition(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    trigger = await service.create_trigger(
        session, admin_ctx, creation_payload(agent_id), request_id=new_uuid7(), ip_hash="h"
    )

    hit = service.test_trigger(trigger, sample_event())
    assert hit.matched is True
    assert hit.event_type_matches is True
    assert [c.passed for c in hit.conditions] == [True, True]

    wrong_team = service.test_trigger(trigger, sample_event(team="OPS"))
    assert wrong_team.matched is False
    assert [c.passed for c in wrong_team.conditions] == [False, True]

    no_transition = service.test_trigger(trigger, sample_event(changed=False))
    assert no_transition.matched is False
    assert no_transition.conditions[1].detail == "field did not change in this event"

    wrong_type = service.test_trigger(
        trigger, {**sample_event(), "event_type": "connector.linear.comment.created"}
    )
    assert wrong_type.matched is False
    assert wrong_type.event_type_matches is False
    assert wrong_type.filter_matches is True  # filter itself still passes


async def test_invocation_listing_and_last_invocation(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent_id: UUID
) -> None:
    trigger = await service.create_trigger(
        session, admin_ctx, creation_payload(agent_id), request_id=new_uuid7(), ip_hash="h"
    )
    for index, status_value in enumerate(("started", "duplicate")):
        session.add(
            TriggerInvocation(
                workspace_id=admin_ctx.workspace_id,
                trigger_id=trigger.id,
                idempotency_key=f"key-{index}",
                event_id=new_uuid7(),
                status=status_value,
            )
        )
    await session.flush()

    rows = await service.list_invocations(session, admin_ctx.workspace_id, trigger.id)
    assert len(rows) == 2
    latest = await service.last_invocations(session, admin_ctx.workspace_id, [trigger.id])
    assert latest[trigger.id].id == rows[0].id
