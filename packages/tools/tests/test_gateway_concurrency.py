"""Portable approval claim and replay concurrency regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_tools.gateway as gateway_module
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


class _SignallingLock(asyncio.Lock):
    """Real asyncio lock that signals when a contended acquire is queued."""

    def __init__(self) -> None:
        super().__init__()
        self.waiter_queued = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.waiter_queued.set()
        return await super().acquire()


async def test_two_resolvers_cannot_execute_one_approval_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third resolver forces the fair-lock wake gap behind resolver two."""
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
    async with (
        sessions() as first_session,
        sessions() as second_session,
        sessions() as third_session,
    ):
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
        third_context = ToolExecutionContext(
            session=third_session,
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        first_gateway = ToolGateway(first_context, catalog)
        second_gateway = ToolGateway(second_context, catalog)
        third_gateway = ToolGateway(third_context, catalog)

        original_first_commit = first_session.commit
        first_commit_count = 0
        allow_third_return = asyncio.Event()

        async def observed_first_commit() -> None:
            nonlocal first_commit_count
            await original_first_commit()
            first_commit_count += 1
            if first_commit_count == 3:
                allow_third_return.set()

        monkeypatch.setattr(first_session, "commit", observed_first_commit)

        original_second_expire_all = second_session.expire_all
        second_expired_before_wait = asyncio.Event()

        def observed_second_expire_all() -> None:
            original_second_expire_all()
            second_expired_before_wait.set()

        monkeypatch.setattr(second_session, "expire_all", observed_second_expire_all)

        original_second_load = second_gateway._load_approval_pair
        second_first_load_completed = asyncio.Event()
        second_load_count = 0

        async def observed_second_load(
            requested_approval_id: UUID,
        ) -> tuple[Approval, ToolCall]:
            nonlocal second_load_count
            pair = await original_second_load(requested_approval_id)
            second_load_count += 1
            if second_load_count == 1:
                assert pair[1].status == ToolCallStatus.EXECUTING.value
                second_first_load_completed.set()
            return pair

        monkeypatch.setattr(second_gateway, "_load_approval_pair", observed_second_load)

        original_third_load = third_gateway._load_approval_pair
        third_first_load_completed = asyncio.Event()
        third_load_count = 0

        async def observed_third_load(
            requested_approval_id: UUID,
        ) -> tuple[Approval, ToolCall]:
            nonlocal third_load_count
            pair = await original_third_load(requested_approval_id)
            third_load_count += 1
            if third_load_count == 1:
                assert pair[1].status == ToolCallStatus.EXECUTING.value
                third_first_load_completed.set()
                await allow_third_return.wait()
            return pair

        monkeypatch.setattr(third_gateway, "_load_approval_pair", observed_third_load)

        first_task = asyncio.create_task(first_gateway.resolve_approved(approval_id))
        await asyncio.wait_for(executor_started.wait(), timeout=5)

        second_task = asyncio.create_task(second_gateway.resolve_approved(approval_id))
        await asyncio.wait_for(second_first_load_completed.wait(), timeout=5)
        await asyncio.wait_for(second_expired_before_wait.wait(), timeout=5)
        assert second_task.done() is False

        third_task = asyncio.create_task(third_gateway.resolve_approved(approval_id))
        await asyncio.wait_for(third_first_load_completed.wait(), timeout=5)
        assert third_task.done() is False

        release_executor.set()
        results = await asyncio.gather(first_task, second_task, third_task)

    assert first_commit_count == 3
    assert all(isinstance(result, GatewayOutcome) for result in results)

    async with sessions() as verification:
        row = await verification.get(ToolCall, parked.tool_call_id)
        assert row is not None
        claimed = await verification.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "tool.call.claimed")
        )
        execution_unknown = await verification.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "tool.call.execution_unknown"
            )
        )
        assert claimed == 1
        assert (
            [result.status for result in results],
            [result.replayed for result in results],
            row.status,
            execution_unknown,
            executions,
        ) == (
            ["executed", "executed", "executed"],
            [False, True, True],
            ToolCallStatus.COMPLETED.value,
            0,
            1,
        )

    async with gateway_module._PROCESS_INVOCATION_LOCKS_GUARD:
        assert parked.tool_call_id not in gateway_module._PROCESS_INVOCATION_LOCKS
        assert parked.tool_call_id not in gateway_module._PROCESS_INVOCATION_LOCK_ENTRANTS

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


async def test_cancelled_queued_entrant_releases_process_lock_state(tmp_path: Path) -> None:
    invocation_id = new_uuid7()
    lock = _SignallingLock()
    async with gateway_module._PROCESS_INVOCATION_LOCKS_GUARD:
        assert invocation_id not in gateway_module._PROCESS_INVOCATION_LOCKS
        gateway_module._PROCESS_INVOCATION_LOCKS[invocation_id] = lock

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancelled-entrant.db'}")
    session = AsyncSession(bind=engine)
    context = ToolExecutionContext(
        session=session,
        workspace_id=new_uuid7(),
        task_id=new_uuid7(),
        run_id=new_uuid7(),
        agent_id=new_uuid7(),
        agent_name="Cancellation",
    )
    gateway = ToolGateway(context, ToolCatalog())
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()

    async def hold_owner_lease() -> None:
        async with gateway._invocation_lifecycle_lock(invocation_id):
            owner_entered.set()
            await release_owner.wait()

    async def queue_lease() -> None:
        async with gateway._invocation_lifecycle_lock(invocation_id):
            raise AssertionError("cancelled queued entrant acquired the lock")

    owner_task = asyncio.create_task(hold_owner_lease())
    queued_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(owner_entered.wait(), timeout=5)
        queued_task = asyncio.create_task(queue_lease())
        await asyncio.wait_for(lock.waiter_queued.wait(), timeout=5)
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        release_owner.set()
        await asyncio.wait_for(owner_task, timeout=5)

        async with gateway_module._PROCESS_INVOCATION_LOCKS_GUARD:
            assert invocation_id not in gateway_module._PROCESS_INVOCATION_LOCKS
            assert invocation_id not in gateway_module._PROCESS_INVOCATION_LOCK_ENTRANTS
    finally:
        release_owner.set()
        pending_tasks = [
            task for task in (owner_task, queued_task) if task is not None and not task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        async with gateway_module._PROCESS_INVOCATION_LOCKS_GUARD:
            gateway_module._PROCESS_INVOCATION_LOCKS.pop(invocation_id, None)
            gateway_module._PROCESS_INVOCATION_LOCK_ENTRANTS.pop(invocation_id, None)
        await session.close()
        await engine.dispose()
