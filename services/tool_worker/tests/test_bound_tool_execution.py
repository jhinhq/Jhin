"""Scalar manifest binding and deterministic ordinary tool execution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from temporalio.exceptions import ApplicationError

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Connection,
    RunEvent,
    Task,
    Workspace,
)
from jhin_domain import ConnectionStatus, RunStatus, new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.activities import ToolActivities, bound_manifest_entry_statement
from jhin_tools import (
    ToolCatalog,
    ToolExecutionContext,
    ToolExecutionError,
    stable_tool_invocation_id,
)
from jhin_workflows.agent_task.shared import ExecuteBoundToolInput


class _EffectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class _ConnectedEffectInput(_EffectInput):
    connection_id: str


class _EffectOutput(BaseModel):
    receipt: str


@dataclass
class _Effect:
    values: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.values)


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    crypto: None = None
    test_barrier: None = None


@dataclass
class ToolWorld:
    activities: ToolActivities
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    effect: _Effect
    workspace: Workspace
    agent: Agent
    task: Task
    run: AgentRun
    connection: Connection
    workspace_override: UUID | None = None
    ordinal_override: int | None = None

    def execute_params(self, *, ordinal: int | None = None) -> ExecuteBoundToolInput:
        return ExecuteBoundToolInput(
            workspace_id=str(self.workspace_override or self.workspace.id),
            run_id=str(self.run.id),
            step_index=2,
            ordinal=(
                ordinal
                if ordinal is not None
                else self.ordinal_override
                if self.ordinal_override is not None
                else 0
            ),
        )

    async def seed_manifest(
        self,
        *,
        calls: list[tuple[str, dict[str, Any] | str]],
    ) -> None:
        entries: list[dict[str, Any]] = []
        for ordinal, (tool_name, arguments) in enumerate(calls):
            arguments_json = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(
                    arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            entries.append(
                {
                    "ordinal": ordinal,
                    "lossless": True,
                    "tool_name": tool_name,
                    "arguments_json": arguments_json,
                }
            )
        async with self.sessions() as session:
            await session.execute(
                delete(RunEvent).where(
                    RunEvent.run_id == self.run.id,
                    RunEvent.event_type == "agent.step.tool_manifest",
                )
            )
            session.add(
                RunEvent(
                    workspace_id=self.workspace.id,
                    run_id=self.run.id,
                    task_id=self.task.id,
                    seq=0,
                    event_type="agent.step.tool_manifest",
                    payload_json={
                        "step": 2,
                        "manifest": {"count": len(entries), "calls": entries},
                    },
                )
            )
            await session.commit()

    async def seed_two_call_manifest(self, values: list[str]) -> None:
        await self.seed_manifest(calls=[("system.echo", {"value": value}) for value in values])

    async def seed_private_reasoning_event(self, payload: dict[str, Any]) -> None:
        async with self.sessions() as session:
            session.add(
                RunEvent(
                    workspace_id=self.workspace.id,
                    run_id=self.run.id,
                    task_id=self.task.id,
                    seq=1,
                    event_type="agent.step.reasoning",
                    payload_json={"step": 2, **payload},
                )
            )
            await session.commit()

    async def arrange_invalid_case(self, case: str) -> None:
        if case == "revoked_grant":
            async with self.sessions() as session:
                await session.execute(
                    delete(AgentCapabilityGrant).where(
                        AgentCapabilityGrant.agent_id == self.agent.id,
                        AgentCapabilityGrant.capability == "system.echo",
                    )
                )
                await session.commit()
            return
        if case == "disabled_connection":
            async with self.sessions() as session:
                connection = await session.get(Connection, self.connection.id)
                assert connection is not None
                connection.status = ConnectionStatus.DISABLED.value
                await session.commit()
            await self.seed_manifest(
                calls=[
                    (
                        "linear.issue.get",
                        {"connection_id": str(self.connection.id), "value": "ordinary"},
                    )
                ]
            )
            return
        if case == "unknown_tool":
            await self.seed_manifest(calls=[("system.unknown", {"value": "ordinary"})])
            return
        if case == "invalid_arguments":
            await self.seed_manifest(calls=[("system.echo", {"value": 7})])
            return
        if case == "wrong_workspace":
            self.workspace_override = new_uuid7()
            return
        if case == "wrong_ordinal":
            self.ordinal_override = 1
            return
        raise AssertionError(f"unknown invalid case: {case}")


@pytest.fixture
async def world() -> AsyncIterator[ToolWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    effect = _Effect()

    async def execute_effect(_ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        parsed = _EffectInput.model_validate(payload.model_dump())
        effect.values.append(parsed.value)
        return _EffectOutput(receipt=f"receipt-{len(effect.values)}")

    async def execute_connected_effect(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        parsed = _ConnectedEffectInput.model_validate(payload.model_dump())
        connection = await ctx.session.get(Connection, UUID(parsed.connection_id))
        if connection is None or connection.status != ConnectionStatus.ACTIVE.value:
            raise ToolExecutionError(
                "connection is unavailable",
                code="connection_unavailable",
                side_effect_possible=False,
            )
        effect.values.append(parsed.value)
        return _EffectOutput(receipt=f"receipt-{len(effect.values)}")

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="system.echo",
            description="Deterministic test effect",
            risk=RiskLevel.WRITE,
            input_model=_EffectInput,
            output_model=_EffectOutput,
            required_capability="system.echo",
        ),
        execute_effect,
    )
    catalog.register(
        ToolDefinition(
            name="linear.issue.get",
            description="Connection-bound deterministic test effect",
            risk=RiskLevel.READ,
            input_model=_ConnectedEffectInput,
            output_model=_EffectOutput,
            required_capability="linear.issue.get",
            scope_keys=("connection_id",),
        ),
        execute_connected_effect,
    )

    async with sessions() as session:
        workspace = Workspace(name="Bound tools", slug=f"bound-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Bound agent", slug="bound-agent")
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Execute a bound tool",
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
        connection = Connection(
            workspace_id=workspace.id,
            connector_type="linear",
            name="Linear test",
            auth_type="api_key",
            status=ConnectionStatus.ACTIVE.value,
        )
        session.add(connection)
        for capability in ("system.echo", "linear.issue.get"):
            session.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json={},
                    effect="allow",
                )
            )
        await session.commit()

    tool_world = ToolWorld(
        activities=ToolActivities(_Resources(sessions), catalog),  # type: ignore[arg-type]
        sessions=sessions,
        engine=engine,
        effect=effect,
        workspace=workspace,
        agent=agent,
        task=task,
        run=run,
        connection=connection,
    )
    await tool_world.seed_manifest(calls=[("system.echo", {"value": "ordinary"})])
    yield tool_world
    await engine.dispose()


async def test_advertise_then_execute_only_the_bound_ordinal(world: ToolWorld) -> None:
    params = world.execute_params()
    assert set(vars(params)) == {"workspace_id", "run_id", "step_index", "ordinal"}

    result = await world.activities.execute_bound_tool_activity(params)

    assert result.tool_call_id == str(stable_tool_invocation_id(world.run.id, 2, 0))
    assert result.status == "executed"
    assert world.effect.count == 1


async def test_retry_reuses_durable_tool_call_without_repeating_effect(world: ToolWorld) -> None:
    first = await world.activities.execute_bound_tool_activity(world.execute_params())
    second = await world.activities.execute_bound_tool_activity(world.execute_params())

    assert first == second
    assert world.effect.values == ["ordinary"]


@pytest.mark.parametrize(
    "case",
    [
        "revoked_grant",
        "disabled_connection",
        "unknown_tool",
        "invalid_arguments",
        "wrong_workspace",
        "wrong_ordinal",
    ],
)
async def test_invalid_live_or_bound_state_stops_before_effect(
    world: ToolWorld,
    case: str,
) -> None:
    await world.arrange_invalid_case(case)

    with pytest.raises(ApplicationError):
        await world.activities.execute_bound_tool_activity(world.execute_params())

    assert world.effect.count == 0


async def test_two_bound_calls_execute_in_manifest_order(world: ToolWorld) -> None:
    await world.seed_two_call_manifest(values=["first", "second"])

    results = [
        await world.activities.execute_bound_tool_activity(world.execute_params(ordinal=ordinal))
        for ordinal in (0, 1)
    ]

    assert [result.tool_call_id for result in results] == [
        str(stable_tool_invocation_id(world.run.id, 2, ordinal)) for ordinal in (0, 1)
    ]
    assert world.effect.values == ["first", "second"]


def test_manifest_statement_projects_only_requested_call_scalars(world: ToolWorld) -> None:
    statement = bound_manifest_entry_statement(world.execute_params(ordinal=1))

    assert tuple(column.key for column in statement.selected_columns) == (
        "ordinal",
        "lossless",
        "tool_name",
        "arguments_json",
    )
    assert all(column is not RunEvent.payload_json for column in statement.selected_columns)


async def test_execution_never_reads_agent_reasoning_event(world: ToolWorld) -> None:
    marker = "must-not-enter-tool-process"
    await world.seed_private_reasoning_event(
        {
            "completion_sanitized": marker,
            "provider_call_ids": [marker],
            "transitions": [{"private": marker}],
            "usage": {"input_tokens": 99},
        }
    )
    observed_sql: list[tuple[str, Any]] = []

    def observe_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "run_event" in statement:
            observed_sql.append((statement, parameters))

    event.listen(world.engine.sync_engine, "before_cursor_execute", observe_sql)
    try:
        await world.activities.execute_bound_tool_activity(world.execute_params())
    finally:
        event.remove(world.engine.sync_engine, "before_cursor_execute", observe_sql)

    assert len(observed_sql) == 1
    assert "agent.step.tool_manifest" in repr(observed_sql[0][1])
    assert "agent.step.reasoning" not in repr(observed_sql)
    assert marker not in repr(observed_sql)
    assert world.effect.values == ["ordinary"]
