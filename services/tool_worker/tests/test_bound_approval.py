"""Durable approval authority at the tool-worker boundary."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

from jhin_db import create_engine as create_database_engine
from jhin_db import create_session_factory
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    Connection,
    RunEvent,
    Secret,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import (
    ApprovalStatus,
    ConnectionStatus,
    RunStatus,
    SecretType,
    ToolCallStatus,
    new_uuid7,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities
from jhin_tools import ToolCatalog, ToolExecutionContext
from jhin_workflows.agent_task.shared import (
    BoundToolResult,
    ExecuteBoundToolInput,
    ResolveBoundToolApprovalInput,
)


class _ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str
    connection_id: str | None = None


class _ApprovalOutput(BaseModel):
    receipt: str


@dataclass
class _Effect:
    count: int = 0


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    crypto: None = None
    test_barrier: None = None


@dataclass
class ApprovalWorld:
    activities: ToolActivities
    sessions: async_sessionmaker[AsyncSession]
    effect: _Effect
    workspace: Workspace
    agent: Agent
    task: Task
    run: AgentRun
    connection: Connection

    def execute_params(self) -> ExecuteBoundToolInput:
        return ExecuteBoundToolInput(
            workspace_id=str(self.workspace.id),
            run_id=str(self.run.id),
            step_index=2,
            ordinal=0,
        )

    def approval_params(self, approval_id: str) -> ResolveBoundToolApprovalInput:
        return ResolveBoundToolApprovalInput(
            workspace_id=str(self.workspace.id),
            task_id=str(self.task.id),
            run_id=str(self.run.id),
            agent_id=str(self.agent.id),
            approval_id=approval_id,
        )

    async def seed_manifest(self, *, with_connection: bool) -> None:
        arguments: dict[str, str] = {"value": "approved"}
        if with_connection:
            arguments["connection_id"] = str(self.connection.id)
        async with self.sessions() as session:
            session.add(
                RunEvent(
                    workspace_id=self.workspace.id,
                    run_id=self.run.id,
                    task_id=self.task.id,
                    seq=0,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "step": 2,
                        "manifest": {
                            "count": 1,
                            "calls": [
                                {
                                    "ordinal": 0,
                                    "lossless": True,
                                    "tool_name": "test.approved.effect",
                                    "arguments_json": json.dumps(
                                        arguments,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                }
                            ],
                        },
                    },
                )
            )
            await session.commit()

    async def approve_in_database(self, approval_id: str) -> None:
        await self.decide_in_database(approval_id, ApprovalStatus.APPROVED.value)

    async def decide_in_database(self, approval_id: str, decision: str) -> None:
        async with self.sessions() as session:
            approval = await session.get(Approval, UUID(approval_id))
            assert approval is not None
            approval.status = decision
            approval.decided_at = datetime.now(UTC)
            await session.commit()

    async def rotate_connection(self) -> None:
        async with self.sessions() as session:
            connection = await session.get(Connection, self.connection.id)
            assert connection is not None
            connection.status = ConnectionStatus.DISABLED.value
            await session.commit()

    async def park_and_approve(self, *, with_connection: bool = False) -> BoundToolResult:
        return await self.park_and_decide(
            decision=ApprovalStatus.APPROVED.value,
            with_connection=with_connection,
        )

    async def park_and_decide(
        self,
        *,
        decision: str,
        with_connection: bool = False,
    ) -> BoundToolResult:
        await self.seed_manifest(with_connection=with_connection)
        parked = await self.activities.execute_bound_tool_activity(self.execute_params())
        assert parked.status == "needs_approval"
        assert parked.approval_id is not None
        await self.decide_in_database(parked.approval_id, decision)
        return parked

    async def seed_executing_claim(self, tool_call_id: str) -> None:
        async with self.sessions() as session:
            row = await session.get(ToolCall, UUID(tool_call_id))
            assert row is not None
            row.status = ToolCallStatus.EXECUTING.value
            await session.commit()

    async def corrupt_run_owner(self) -> None:
        async with self.sessions() as session:
            replacement = Agent(
                workspace_id=self.workspace.id,
                name="Replacement owner",
                slug=f"replacement-{new_uuid7().hex[:8]}",
            )
            session.add(replacement)
            await session.flush()
            run = await session.get(AgentRun, self.run.id)
            assert run is not None
            run.agent_id = replacement.id
            await session.commit()


async def _build_world(
    sessions: async_sessionmaker[AsyncSession],
) -> ApprovalWorld:
    effect = _Effect()

    async def execute_effect(_ctx: ToolExecutionContext, _payload: BaseModel) -> BaseModel:
        effect.count += 1
        return _ApprovalOutput(receipt=f"receipt-{effect.count}")

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.approved.effect",
            description="Approval-gated deterministic effect",
            risk=RiskLevel.ELEVATED,
            input_model=_ApprovalInput,
            output_model=_ApprovalOutput,
            required_capability="test.approved.effect",
            supports_approval=True,
            scope_keys=("connection_id",),
        ),
        execute_effect,
    )

    async with sessions() as session:
        workspace = Workspace(name="Approvals", slug=f"approvals-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Approval agent", slug="approval-agent")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Resolve one approval",
            assigned_agent_id=agent.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        session.add(run)
        secret = Secret(
            workspace_id=workspace.id,
            name=f"approval-secret-{new_uuid7().hex[:8]}",
            type=SecretType.CONNECTION_CREDENTIALS.value,
            ciphertext=b"ciphertext",
            nonce=b"nonce",
            wrapped_data_key=b"wrapped-key",
            key_version=1,
            secret_fingerprint="a" * 64,
        )
        session.add(secret)
        await session.flush()
        connection = Connection(
            workspace_id=workspace.id,
            connector_type="example",
            name="Approval connection",
            auth_type="api_key",
            status=ConnectionStatus.ACTIVE.value,
            encrypted_secret_id=secret.id,
            config_json={"project": "bound-project"},
        )
        session.add(connection)
        session.add(
            AgentCapabilityGrant(
                workspace_id=workspace.id,
                agent_id=agent.id,
                capability="test.approved.effect",
                scope_json={},
                effect="allow",
            )
        )
        await session.commit()

    return ApprovalWorld(
        activities=ToolActivities(_Resources(sessions), catalog),  # type: ignore[arg-type]
        sessions=sessions,
        effect=effect,
        workspace=workspace,
        agent=agent,
        task=task,
        run=run,
        connection=connection,
    )


@pytest.fixture
async def world() -> AsyncIterator[ApprovalWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield await _build_world(sessions)
    await engine.dispose()


@pytest.fixture
async def postgres_world() -> AsyncIterator[ApprovalWorld]:
    host = os.environ.get("JHIN_POSTGRES_HOST", "127.0.0.1")
    port = int(os.environ.get("POSTGRES_DEV_PORT", "55432"))
    database_name = f"jhin_tool_approval_{uuid4().hex}"
    admin_dsn = f"postgresql://jhin:jhin@{host}:{port}/postgres"
    database_url = f"postgresql+asyncpg://jhin:jhin@{host}:{port}/{database_name}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await admin.close()

    engine = create_database_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = create_session_factory(engine)
        yield await _build_world(sessions)
    finally:
        await engine.dispose()
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                database_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        finally:
            await admin.close()


async def test_approval_resolution_reloads_database_authority_and_identity(
    world: ApprovalWorld,
) -> None:
    parked = await world.park_and_approve(with_connection=True)
    await world.rotate_connection()

    result = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )

    assert result.tool_call_id == parked.tool_call_id
    assert result.status == "denied"
    assert world.effect.count == 0


async def test_ambiguous_approved_effect_returns_durable_unknown(world: ApprovalWorld) -> None:
    parked = await world.park_and_approve()
    await world.seed_executing_claim(parked.tool_call_id)

    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )
    second = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id or "")
    )

    assert (
        first
        == second
        == BoundToolResult(
            tool_call_id=parked.tool_call_id,
            status="execution_unknown",
            approval_id=parked.approval_id,
            stop_reason="execution_unknown",
        )
    )
    assert world.effect.count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_persisted_status", "expected_effect_count"),
    [
        (
            ApprovalStatus.APPROVED.value,
            "executed",
            ToolCallStatus.COMPLETED.value,
            1,
        ),
        (
            ApprovalStatus.REJECTED.value,
            "rejected",
            ToolCallStatus.REJECTED.value,
            0,
        ),
    ],
)
async def test_postgres_resolution_survives_gateway_rollback_and_replays_without_effect(
    postgres_world: ApprovalWorld,
    decision: str,
    expected_status: str,
    expected_persisted_status: str,
    expected_effect_count: int,
) -> None:
    world = postgres_world
    parked = await world.park_and_decide(decision=decision)
    assert parked.approval_id is not None

    first = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )
    replay = await world.activities.resolve_bound_tool_approval_activity(
        world.approval_params(parked.approval_id)
    )

    assert (
        first
        == replay
        == BoundToolResult(
            tool_call_id=parked.tool_call_id,
            status=expected_status,
            approval_id=parked.approval_id,
        )
    )
    assert world.effect.count == expected_effect_count
    async with world.sessions() as session:
        tool_call = await session.get(ToolCall, UUID(parked.tool_call_id))
        assert tool_call is not None
        assert tool_call.status == expected_persisted_status


async def test_approval_rejects_run_owner_drift_before_effect(world: ApprovalWorld) -> None:
    parked = await world.park_and_approve()
    assert parked.approval_id is not None
    await world.corrupt_run_owner()

    with pytest.raises(ApplicationError) as error:
        await world.activities.resolve_bound_tool_approval_activity(
            world.approval_params(parked.approval_id)
        )

    assert error.value.type == "approval_context_not_found"
    assert error.value.non_retryable is True
    assert world.effect.count == 0
