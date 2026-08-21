"""Portable two-session approval claim and replay concurrency regression."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import Agent, AgentCapabilityGrant, Approval, AuditEvent, ToolCall, Workspace
from jhin_domain import ApprovalStatus, ToolCallStatus, new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import GatewayOutcome, ToolGateway


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class _Output(BaseModel):
    executed: bool


async def test_two_resolvers_cannot_execute_one_approval_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'approval-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    executions = 0
    executor_started = asyncio.Event()
    release_executor = asyncio.Event()

    async def executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        nonlocal executions
        executions += 1
        executor_started.set()
        await release_executor.wait()
        return _Output(executed=True)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.concurrent_approval",
            description="Concurrency test",
            risk=RiskLevel.ELEVATED,
            input_model=_Input,
            output_model=_Output,
            required_capability="test.concurrent_approval",
            supports_approval=True,
        ),
        executor,
    )

    async with sessions() as setup:
        workspace = Workspace(name="Race", slug=f"race-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        agent = Agent(workspace_id=workspace.id, name="Racer", slug="racer")
        setup.add(agent)
        await setup.flush()
        task_id = new_uuid7()
        run_id = new_uuid7()
        setup.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability="test.concurrent_approval",
                scope_json={},
                effect="allow",
            )
        )
        context = ToolExecutionContext(
            session=setup,
            workspace_id=workspace.id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        parked = await ToolGateway(context, catalog).request(
            "test.concurrent_approval",
            '{"label": "once"}',
            provider_call_id="call-race",
        )
        assert parked.approval_id is not None
        approval = await setup.get(Approval, parked.approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.APPROVED.value
        await setup.commit()
        identity = (workspace.id, task_id, run_id, agent.id, agent.name, parked.approval_id)

    workspace_id, task_id, run_id, agent_id, agent_name, approval_id = identity
    async with sessions() as first_session, sessions() as second_session:
        first_context = ToolExecutionContext(
            session=first_session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        second_context = ToolExecutionContext(
            session=second_session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        second_gateway = ToolGateway(second_context, catalog)
        original_load_approval_pair = second_gateway._load_approval_pair
        second_first_load_completed = asyncio.Event()
        second_load_count = 0

        async def observed_load_approval_pair(
            requested_approval_id: UUID,
        ) -> tuple[Approval, ToolCall]:
            nonlocal second_load_count
            pair = await original_load_approval_pair(requested_approval_id)
            second_load_count += 1
            if second_load_count == 1:
                assert pair[1].status == ToolCallStatus.EXECUTING.value
                second_first_load_completed.set()
            return pair

        monkeypatch.setattr(second_gateway, "_load_approval_pair", observed_load_approval_pair)
        first_task = asyncio.create_task(
            ToolGateway(first_context, catalog).resolve_approved(approval_id)
        )
        await asyncio.wait_for(executor_started.wait(), timeout=5)
        second_task = asyncio.create_task(second_gateway.resolve_approved(approval_id))
        await asyncio.wait_for(second_first_load_completed.wait(), timeout=5)
        assert second_task.done() is False
        release_executor.set()
        results = await asyncio.gather(first_task, second_task)
        await first_session.commit()
        await second_session.commit()

    assert all(isinstance(result, GatewayOutcome) for result in results)
    assert [result.status for result in results] == ["executed", "executed"]
    assert [result.replayed for result in results] == [False, True]
    assert executions == 1

    async with sessions() as verification:
        row = await verification.get(ToolCall, parked.tool_call_id)
        assert row is not None
        assert row.status == ToolCallStatus.COMPLETED.value
        claimed = await verification.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "tool.call.claimed")
        )
        assert claimed == 1
        replay_context = ToolExecutionContext(
            session=verification,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        replay = await ToolGateway(replay_context, catalog).resolve_approved(approval_id)
        assert replay.replayed is True
        assert replay.status == "executed"
        assert executions == 1

    await engine.dispose()


async def test_two_rejection_resolvers_replay_one_terminal_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rejection-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        return _Output(executed=True)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.concurrent_rejection",
            description="Concurrent rejection test",
            risk=RiskLevel.ELEVATED,
            input_model=_Input,
            output_model=_Output,
            required_capability="test.concurrent_rejection",
            supports_approval=True,
        ),
        executor,
    )

    async with sessions() as setup:
        workspace = Workspace(name="Rejection", slug=f"rejection-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        agent = Agent(workspace_id=workspace.id, name="Rejector", slug="rejector")
        setup.add(agent)
        await setup.flush()
        task_id = new_uuid7()
        run_id = new_uuid7()
        setup.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability="test.concurrent_rejection",
                scope_json={},
                effect="allow",
            )
        )
        context = ToolExecutionContext(
            session=setup,
            workspace_id=workspace.id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        parked = await ToolGateway(context, catalog).request(
            "test.concurrent_rejection",
            '{"label": "reject"}',
            provider_call_id="call-rejection-race",
        )
        assert parked.approval_id is not None
        approval = await setup.get(Approval, parked.approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.REJECTED.value
        await setup.commit()
        identity = (workspace.id, task_id, run_id, agent.id, agent.name, parked.approval_id)

    workspace_id, task_id, run_id, agent_id, agent_name, approval_id = identity
    async with sessions() as first_session, sessions() as second_session:
        first_context = ToolExecutionContext(
            session=first_session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        second_context = ToolExecutionContext(
            session=second_session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        first_gateway = ToolGateway(first_context, catalog)
        second_gateway = ToolGateway(second_context, catalog)
        first_resolver_entered = asyncio.Event()
        release_first_resolver = asyncio.Event()
        original_resolve_rejected_once = first_gateway._resolve_rejected_once

        async def paused_resolve_rejected_once(
            requested_approval_id: UUID,
        ) -> GatewayOutcome:
            first_resolver_entered.set()
            await release_first_resolver.wait()
            return await original_resolve_rejected_once(requested_approval_id)

        second_first_load_completed = asyncio.Event()
        second_load_count = 0
        original_load_approval_pair = second_gateway._load_approval_pair

        async def observed_load_approval_pair(
            requested_approval_id: UUID,
        ) -> tuple[Approval, ToolCall]:
            nonlocal second_load_count
            pair = await original_load_approval_pair(requested_approval_id)
            second_load_count += 1
            if second_load_count == 1:
                assert pair[1].status == ToolCallStatus.PENDING_APPROVAL.value
                second_first_load_completed.set()
            return pair

        monkeypatch.setattr(first_gateway, "_resolve_rejected_once", paused_resolve_rejected_once)
        monkeypatch.setattr(second_gateway, "_load_approval_pair", observed_load_approval_pair)

        first_task = asyncio.create_task(first_gateway.resolve_rejected(approval_id))
        await asyncio.wait_for(first_resolver_entered.wait(), timeout=5)
        second_task = asyncio.create_task(second_gateway.resolve_rejected(approval_id))
        await asyncio.wait_for(second_first_load_completed.wait(), timeout=5)
        assert second_task.done() is False
        release_first_resolver.set()
        results = await asyncio.gather(first_task, second_task)

    assert [result.status for result in results] == ["rejected", "rejected"]
    assert [result.replayed for result in results] == [False, True]

    async with sessions() as verification:
        rejected = await verification.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "tool.call.rejected")
        )
        assert rejected == 1

    await engine.dispose()
