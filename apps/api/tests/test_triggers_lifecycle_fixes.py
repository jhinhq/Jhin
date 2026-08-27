"""A trigger is only as alive as the agent it assigns work to.

Deleting that agent detaches it (FK ``SET NULL``) and used to leave the
trigger enabled, failing every matching event with an internal code; pausing
it produced the same opaque code. These cover the switch-off, the refusal to
switch a targetless trigger back on, and the plain-language outcomes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.triggers import service
from jhin_api.triggers.schemas import TriggerCreate
from jhin_db.models import Agent, AuditEvent, Team, Trigger, TriggerInvocation
from jhin_domain import AgentStatus, TriggerInvocationStatus, new_uuid7
from jhin_observability import SafeErrorCode

REQ: dict[str, Any] = {"request_id": new_uuid7(), "ip_hash": "test"}


@pytest.fixture
async def agent(session: AsyncSession, admin_ctx: WorkspaceContext) -> Agent:
    row = Agent(workspace_id=admin_ctx.workspace_id, name="Sam", slug="sam")
    session.add(row)
    await session.flush()
    return row


async def _trigger(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    target_agent_id: UUID | None = None,
    **extra: Any,
) -> Trigger:
    payload = TriggerCreate(
        name="Pick up new tickets",
        event_type="connector.linear.issue.updated",
        target_agent_id=target_agent_id,
        **extra,
    )
    return await service.create_trigger(session, ctx, payload, **REQ)


async def _failed_invocation(
    session: AsyncSession, ctx: WorkspaceContext, trigger: Trigger, *, error: str
) -> TriggerInvocation:
    invocation = TriggerInvocation(
        workspace_id=ctx.workspace_id,
        trigger_id=trigger.id,
        idempotency_key=f"key-{new_uuid7().hex[:8]}",
        event_id=new_uuid7(),
        status=TriggerInvocationStatus.FAILED.value,
        error=error,
    )
    session.add(invocation)
    await session.commit()
    return invocation


# --- The target is gone -------------------------------------------------


async def test_a_trigger_whose_agent_was_deleted_is_switched_off(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    # What Postgres leaves behind when the agent row goes: a detached trigger.
    trigger.target_agent_id = None
    await session.delete(agent)
    await session.commit()

    health = await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    assert trigger.enabled is False
    assert health[trigger.id].state == service.TARGET_AGENT_DELETED
    warning = health[trigger.id].warning
    assert warning is not None and "deleted" in warning and "choose another agent" in warning
    audit_row = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "trigger.disabled", AuditEvent.target_id == trigger.id
        )
    )
    assert audit_row is not None
    assert audit_row.metadata_json["reason"] == "target agent no longer exists"


async def test_a_trigger_pointing_at_a_removed_agent_is_switched_off(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Databases that leave the id in place must reach the same conclusion."""
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    await session.delete(agent)
    await session.commit()

    health = await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    assert trigger.enabled is False
    assert health[trigger.id].state == service.TARGET_AGENT_DELETED


async def test_switching_a_targetless_trigger_back_on_is_refused(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    trigger.target_agent_id = None
    await session.delete(agent)
    await session.commit()
    await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    with pytest.raises(HTTPException) as excinfo:
        await service.set_enabled(session, admin_ctx, trigger.id, enabled=True, **REQ)

    detail = str(excinfo.value.detail)
    assert excinfo.value.status_code == 422
    assert "no agent to give work to" in detail
    assert "Edit it to choose another agent" in detail
    assert trigger.enabled is False


async def test_reconciling_leaves_a_healthy_trigger_alone(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)

    health = await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    assert trigger.enabled is True
    assert health[trigger.id].state == service.TARGET_OK
    assert health[trigger.id].warning is None


# --- The target is paused ------------------------------------------------


async def test_a_paused_agent_is_reported_without_switching_the_trigger_off(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    """Pausing is temporary, so the trigger stays as the admin left it — but
    the reason nothing runs has to be visible."""
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    agent.status = AgentStatus.PAUSED.value
    await session.commit()

    health = await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    assert trigger.enabled is True
    assert health[trigger.id].state == service.TARGET_AGENT_PAUSED
    warning = health[trigger.id].warning
    assert warning is not None and "paused" in warning and "Resume that agent" in warning


async def test_a_failure_says_the_agent_is_paused_rather_than_an_error_code(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    agent.status = AgentStatus.PAUSED.value
    await session.commit()
    invocation = await _failed_invocation(
        session, admin_ctx, trigger, error=SafeErrorCode.INVALID_REQUEST.value
    )

    health = await service.target_health(session, admin_ctx.workspace_id, trigger)
    message = service.invocation_message(invocation, health)

    assert message is not None
    assert "paused" in message
    assert SafeErrorCode.INVALID_REQUEST.value not in message


async def test_an_unexplained_failure_still_avoids_the_stored_code(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    for code in (
        SafeErrorCode.INVALID_REQUEST.value,
        SafeErrorCode.UPSTREAM_UNAVAILABLE.value,
        "something_new",
    ):
        invocation = await _failed_invocation(session, admin_ctx, trigger, error=code)
        message = service.invocation_message(invocation, None)
        assert message is not None
        assert code not in message


async def test_a_successful_invocation_carries_no_failure_message(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    trigger = await _trigger(session, admin_ctx, target_agent_id=agent.id)
    invocation = TriggerInvocation(
        workspace_id=admin_ctx.workspace_id,
        trigger_id=trigger.id,
        idempotency_key="key-started",
        event_id=new_uuid7(),
        status=TriggerInvocationStatus.STARTED.value,
    )
    session.add(invocation)
    await session.commit()

    assert service.invocation_message(invocation, None) is None


# --- Team targets --------------------------------------------------------


async def test_a_team_with_no_active_agent_is_reported(
    session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
) -> None:
    team = Team(workspace_id=admin_ctx.workspace_id, name="Support")
    session.add(team)
    await session.flush()
    agent.team_id = team.id
    trigger = await _trigger(session, admin_ctx, target_team_id=team.id)
    agent.status = AgentStatus.PAUSED.value
    await session.commit()

    health = await service.reconcile_targets(session, admin_ctx.workspace_id, [trigger])

    assert trigger.enabled is True
    assert health[trigger.id].state == service.TARGET_TEAM_UNSTAFFED
    warning = health[trigger.id].warning
    assert warning is not None and "no active agent" in warning
