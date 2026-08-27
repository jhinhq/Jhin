"""Deleting an agent must not leave automations pointing at nothing.

The FK is SET NULL, so an enabled trigger survived its target and then failed
on every matching event with nothing to explain why.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.agents import service
from jhin_api.deps import WorkspaceContext
from jhin_db.models import Agent, AuditEvent, Trigger
from jhin_domain import new_uuid7


async def make_agent(session: AsyncSession, ctx: WorkspaceContext, name: str) -> Agent:
    agent = Agent(
        workspace_id=ctx.workspace_id, name=name, slug=name.lower(), role_title="Engineer"
    )
    session.add(agent)
    await session.flush()
    return agent


async def make_trigger(
    session: AsyncSession, ctx: WorkspaceContext, agent: Agent, *, enabled: bool = True
) -> Trigger:
    trigger = Trigger(
        workspace_id=ctx.workspace_id,
        name=f"Nightly for {agent.name}",
        enabled=enabled,
        event_type="connector.linear.issue.updated",
        target_agent_id=agent.id,
    )
    session.add(trigger)
    await session.flush()
    return trigger


@pytest.mark.parametrize("enabled", [True, False])
async def test_delete_switches_off_only_enabled_triggers_for_that_agent(
    session: AsyncSession, admin_ctx: WorkspaceContext, enabled: bool
) -> None:
    doomed = await make_agent(session, admin_ctx, "Ada")
    trigger = await make_trigger(session, admin_ctx, doomed, enabled=enabled)

    await service.delete_agent(session, admin_ctx, doomed.id, request_id=new_uuid7(), ip_hash="h")

    await session.refresh(trigger)
    assert trigger.enabled is False

    reasons = [
        event.metadata_json.get("reason")
        for event in await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "trigger.disabled")
        )
    ]
    # Only a trigger that was actually switched off is worth an audit row.
    assert reasons == (["target agent was deleted"] if enabled else [])


async def test_delete_leaves_another_agents_trigger_running(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> None:
    doomed = await make_agent(session, admin_ctx, "Ada")
    keeper = await make_agent(session, admin_ctx, "Linus")
    theirs = await make_trigger(session, admin_ctx, keeper)

    await service.delete_agent(session, admin_ctx, doomed.id, request_id=new_uuid7(), ip_hash="h")

    await session.refresh(theirs)
    assert theirs.enabled is True
    assert theirs.target_agent_id == keeper.id
