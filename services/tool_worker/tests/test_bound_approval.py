"""Durable approval authority at the tool-worker boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
        async with self.sessions() as session:
            approval = await session.get(Approval, UUID(approval_id))
            assert approval is not None
            approval.status = ApprovalStatus.APPROVED.value
            approval.decided_at = datetime.now(UTC)
            await session.commit()

    async def rotate_connection(self) -> None:
        async with self.sessions() as session:
            connection = await session.get(Connection, self.connection.id)
            assert connection is not None
            connection.status = ConnectionStatus.DISABLED.value
            await session.commit()

    async def park_and_approve(self, *, with_connection: bool = False) -> BoundToolResult:
        await self.seed_manifest(with_connection=with_connection)
        parked = await self.activities.execute_bound_tool_activity(self.execute_params())
        assert parked.status == "needs_approval"
        assert parked.approval_id is not None
        await self.approve_in_database(parked.approval_id)
        return parked

    async def seed_executing_claim(self, tool_call_id: str) -> None:
        async with self.sessions() as session:
            row = await session.get(ToolCall, UUID(tool_call_id))
            assert row is not None
            row.status = ToolCallStatus.EXECUTING.value
            await session.commit()


@pytest.fixture
async def world() -> AsyncIterator[ApprovalWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
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

    yield ApprovalWorld(
        activities=ToolActivities(_Resources(sessions), catalog),  # type: ignore[arg-type]
        sessions=sessions,
        effect=effect,
        workspace=workspace,
        agent=agent,
        task=task,
        run=run,
        connection=connection,
    )
    await engine.dispose()


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

    assert first == second == BoundToolResult(
        tool_call_id=parked.tool_call_id,
        status="execution_unknown",
        approval_id=parked.approval_id,
        stop_reason="execution_unknown",
    )
    assert world.effect.count == 0
