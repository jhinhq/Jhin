"""What the gateway may and may not conclude when it re-enters a claim.

The at-most-once guarantee lives in two states of one row. ``claimed`` says
the executor was never entered and the call may be dispatched; ``executing``
says it was entered and nothing can be proven. These tests are the ones that
would catch the guarantee being weakened: they check both verdicts, the audit
trail that justifies each, and — with the invocation lifecycle lock removed
entirely — that the dispatch compare-and-set alone still admits exactly one
execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AuditEvent,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import RunStatus, ToolCallStatus, new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import GatewayOutcome, ToolGateway
from jhin_tools.invocation import stable_tool_invocation_id


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    connection_id: str | None = None


class _Output(BaseModel):
    executed: bool


@dataclass
class _World:
    sessions: async_sessionmaker[AsyncSession]
    catalog: ToolCatalog
    workspace_id: UUID
    agent_id: UUID
    task_id: UUID
    run_id: UUID
    invocation_id: UUID
    executions: list[str]
    connection_id: str | None = None

    def context(self, session: AsyncSession) -> ToolExecutionContext:
        return ToolExecutionContext(
            session=session,
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            agent_name="Reentrant",
            session_factory=self.sessions,
        )

    def gateway(self, session: AsyncSession) -> ToolGateway:
        return ToolGateway(self.context(session), self.catalog)

    async def request(self, gateway: ToolGateway) -> GatewayOutcome:
        payload = {"label": "once"}
        if self.connection_id is not None:
            payload["connection_id"] = self.connection_id
        return await gateway.request(
            "test.reentry", json.dumps(payload), invocation_id=self.invocation_id
        )

    async def row(self) -> ToolCall:
        async with self.sessions() as verify:
            row = await verify.get(ToolCall, self.invocation_id)
            assert row is not None
            return row

    async def audit_actions(self) -> list[str]:
        async with self.sessions() as verify:
            rows = await verify.scalars(
                select(AuditEvent)
                .where(AuditEvent.target_id == self.invocation_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return [row.action for row in rows]

    async def audit_metadata(self, action: str) -> list[dict[str, Any]]:
        async with self.sessions() as verify:
            rows = await verify.scalars(
                select(AuditEvent).where(
                    AuditEvent.target_id == self.invocation_id,
                    AuditEvent.action == action,
                )
            )
            return [row.metadata_json for row in rows]


@contextlib.asynccontextmanager
async def _world(
    tmp_path: Path,
    name: str,
    *,
    executor: Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]] | None = None,
    executions: list[str] | None = None,
    connected: bool = False,
) -> AsyncIterator[_World]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ran = executions if executions is not None else []

    async def default_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        ran.append("executed")
        return _Output(executed=True)

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="test.reentry",
            description="Re-entry test",
            risk=RiskLevel.WRITE,
            input_model=_Input,
            output_model=_Output,
            required_capability="test.reentry",
        ),
        executor or default_executor,
    )

    async with sessions() as setup:
        workspace = Workspace(name="Reentry", slug=f"reentry-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        workspace_id = workspace.id
        agent = Agent(workspace_id=workspace_id, name="Reentrant", slug="reentrant")
        setup.add(agent)
        await setup.flush()
        task = Task(workspace_id=workspace_id, title="Re-entry", correlation_id=new_uuid7())
        setup.add(task)
        await setup.flush()
        run = AgentRun(
            workspace_id=workspace_id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        setup.add(run)
        setup.add(
            AgentCapabilityGrant(
                workspace_id=workspace_id,
                agent_id=agent.id,
                capability="test.reentry",
                scope_json={},
                effect="allow",
            )
        )
        await setup.commit()
        identity = (workspace_id, agent.id, task.id, run.id)

    workspace_id, agent_id, task_id, run_id = identity
    yield _World(
        sessions=sessions,
        catalog=catalog,
        workspace_id=workspace_id,
        agent_id=agent_id,
        task_id=task_id,
        run_id=run_id,
        invocation_id=stable_tool_invocation_id(run_id, 0, 0),
        executions=ran,
        # A connection id that names no row: the digest an approval must bind
        # to cannot be read, which is exactly the denial under test.
        connection_id=str(new_uuid7()) if connected else None,
    )
    await engine.dispose()


async def _abandon_claim_before_dispatch(world: _World) -> None:
    """Leave a durable claim whose executor was never entered — a worker
    killed between the claim commit and the dispatch compare-and-set."""
    async with world.sessions() as session:
        gateway = world.gateway(session)

        async def die_before_dispatch(*args: Any, **kwargs: Any) -> GatewayOutcome:
            raise asyncio.CancelledError

        gateway._execute = die_before_dispatch  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await world.request(gateway)


async def test_a_claim_that_never_reached_its_executor_is_dispatched_once(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, "undispatched") as world:
        await _abandon_claim_before_dispatch(world)
        abandoned = await world.row()
        assert (abandoned.status, world.executions) == (ToolCallStatus.CLAIMED.value, [])
        assert "tool.call.dispatched" not in await world.audit_actions()

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session))

        finished = await world.row()
        assert (outcome.status, outcome.replayed, finished.status, world.executions) == (
            "executed",
            False,
            ToolCallStatus.COMPLETED.value,
            ["executed"],
        )
        actions = await world.audit_actions()
        assert actions.count("tool.call.claim_reentered") == 1
        assert actions.count("tool.call.dispatched") == 1
        assert actions.count("tool.call.execution_unknown") == 0
        # The verdict names its evidence, not just its conclusion.
        [reentry] = await world.audit_metadata("tool.call.claim_reentered")
        assert reentry["code"] == "claim_not_dispatched"
        assert "claimed" in reentry["evidence"]


async def test_a_claim_that_reached_its_executor_stays_unknown(tmp_path: Path) -> None:
    async with _world(tmp_path, "dispatched") as world:
        # A worker killed *inside* the executor: the fence is committed, so
        # the row says executing and nothing can vouch for what happened.
        async with world.sessions() as session:
            gateway = world.gateway(session)

            async def crash_after_fence(
                definition: ToolDefinition,
                row: ToolCall,
                validated_input: BaseModel,
                *,
                session: AsyncSession,
                **kwargs: Any,
            ) -> GatewayOutcome:
                assert await gateway._mark_dispatched(
                    session,
                    row.id,
                    tool_name=definition.name,
                    risk=definition.risk.value,
                )
                raise asyncio.CancelledError

            gateway._run_executor = crash_after_fence  # type: ignore[method-assign]
            with pytest.raises(asyncio.CancelledError):
                await world.request(gateway)

        dispatched = await world.row()
        assert (dispatched.status, world.executions) == (ToolCallStatus.EXECUTING.value, [])

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session))

        reconciled = await world.row()
        assert (outcome.status, outcome.error_code, reconciled.status, world.executions) == (
            "execution_unknown",
            "execution_outcome_unknown",
            ToolCallStatus.EXECUTION_UNKNOWN.value,
            [],
        )
        actions = await world.audit_actions()
        assert actions.count("tool.call.execution_unknown") == 1
        assert actions.count("tool.call.claim_reentered") == 0
        [unknown] = await world.audit_metadata("tool.call.execution_unknown")
        assert unknown["evidence"] == "the tool_call row was dispatched to its executor"


async def test_two_concurrent_re_entries_dispatch_the_claim_once(tmp_path: Path) -> None:
    async with _world(tmp_path, "concurrent") as world:
        await _abandon_claim_before_dispatch(world)

        async with world.sessions() as first, world.sessions() as second:
            outcomes = await asyncio.gather(
                world.request(world.gateway(first)),
                world.request(world.gateway(second)),
            )

        actions = await world.audit_actions()
        assert world.executions == ["executed"]
        assert actions.count("tool.call.dispatched") == 1
        assert sorted(outcome.status for outcome in outcomes) == ["executed", "executed"]
        assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
        assert (await world.row()).status == ToolCallStatus.COMPLETED.value


async def test_the_dispatch_compare_and_set_alone_admits_one_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifecycle lock is an optimization, not the guarantee.

    Both re-entries are let past the row's status check while it still reads
    ``claimed``, with the lock removed entirely, so the only thing standing
    between them and two pushes is the compare-and-set.
    """
    executor_entered = asyncio.Event()
    release_executor = asyncio.Event()
    executions: list[str] = []

    async def blocking_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        executions.append("executed")
        executor_entered.set()
        await release_executor.wait()
        return _Output(executed=True)

    @contextlib.asynccontextmanager
    async def no_lock(
        self: ToolGateway, invocation_id: UUID, **kwargs: Any
    ) -> AsyncIterator[ToolGateway]:
        yield self

    async with _world(tmp_path, "cas", executor=blocking_executor, executions=executions) as world:
        await _abandon_claim_before_dispatch(world)
        monkeypatch.setattr(ToolGateway, "_invocation_lifecycle_lock", no_lock)

        async with world.sessions() as winner_session, world.sessions() as loser_session:
            winner = world.gateway(winner_session)
            loser = world.gateway(loser_session)

            loser_reached_fence = asyncio.Event()
            release_loser_fence = asyncio.Event()
            original_loser_fence = loser._mark_dispatched

            async def held_fence(*args: Any, **kwargs: Any) -> bool:
                loser_reached_fence.set()
                await release_loser_fence.wait()
                return await original_loser_fence(*args, **kwargs)

            loser._mark_dispatched = held_fence  # type: ignore[method-assign]

            loser_task = asyncio.create_task(world.request(loser))
            await asyncio.wait_for(loser_reached_fence.wait(), timeout=5)
            # The loser is past every status check with the row still
            # ``claimed``; only the compare-and-set is left.
            assert (await world.row()).status == ToolCallStatus.CLAIMED.value

            winner_task = asyncio.create_task(world.request(winner))
            await asyncio.wait_for(executor_entered.wait(), timeout=5)
            release_loser_fence.set()
            loser_outcome = await asyncio.wait_for(loser_task, timeout=5)
            release_executor.set()
            winner_outcome = await asyncio.wait_for(winner_task, timeout=5)

        assert executions == ["executed"]
        assert (await world.audit_actions()).count("tool.call.dispatched") == 1
        assert (winner_outcome.status, loser_outcome.status) == ("executed", "execution_unknown")


async def test_a_re_entered_claim_denied_for_an_unusable_connection_is_adopted(
    tmp_path: Path,
) -> None:
    """Every denial on a re-entered claim writes onto the claim it re-entered.

    ``approval_connection_unavailable`` was the one that did not. It inserted
    a second row under the same invocation id, collided with the unique index,
    and reported ``invocation_mismatch`` — a wrong answer that also left the
    original row stranded in ``claimed`` for nobody to reconcile.

    The scenario is the one that call site exists for: a claim taken while the
    call ran automatically, re-entered after policy started asking for a
    person — and the connection the approval would have to bind to is gone.
    """
    async with _world(tmp_path, "approval_connection", connected=True) as world:
        await _abandon_claim_before_dispatch(world)
        assert (await world.row()).status == ToolCallStatus.CLAIMED.value

        # Policy now wants a person to see this call.
        approving = ToolCatalog()
        approving.register(
            ToolDefinition(
                name="test.reentry",
                description="Re-entry test",
                risk=RiskLevel.ELEVATED,
                input_model=_Input,
                output_model=_Output,
                required_capability="test.reentry",
                supports_approval=True,
            ),
            _unreachable_executor,
        )
        async with world.sessions() as session:
            outcome = await world.request(ToolGateway(world.context(session), approving))

        row = await world.row()
        assert outcome.status == "denied"
        assert outcome.error_code == "approval_connection_unavailable"
        assert outcome.tool_call_id == world.invocation_id
        assert row.status == ToolCallStatus.DENIED.value
        assert row.error_code == "approval_connection_unavailable"
        assert world.executions == []
        assert "tool.call.denied" in await world.audit_actions()


async def _unreachable_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
    raise AssertionError("a denied call must never reach its executor")


async def test_a_re_entered_claim_whose_tool_was_revoked_is_denied_not_mismatched(
    tmp_path: Path,
) -> None:
    """A claim nothing dispatched, re-entered after the tool went away.

    The claim's stored input is the model dump of a *validated* payload, and
    this re-entry cannot validate anything — the tool is not in the catalog
    any more, so there is no schema to dump through. Comparing the raw
    arguments against that dump therefore compared unequal on every such
    re-entry and answered ``invocation_mismatch``: a shape that says "this id
    belongs to some other call" about a row that is this call, sending a
    person to reconcile a collision that never happened and leaving the claim
    stranded in ``claimed`` for nobody.

    Identity is established from the durable execution context, and the row is
    provably undispatched, so the honest ending is the denial the attempt was
    recording — written onto the claim it re-entered.
    """
    async with _world(tmp_path, "revoked_tool") as world:
        await _abandon_claim_before_dispatch(world)
        assert (await world.row()).status == ToolCallStatus.CLAIMED.value

        async with world.sessions() as session:
            outcome = await ToolGateway(world.context(session), ToolCatalog()).request(
                "test.reentry",
                json.dumps({"label": "once"}),
                invocation_id=world.invocation_id,
            )

        row = await world.row()
        assert outcome.status == "denied"
        assert outcome.error_code == "tool_not_found"
        assert outcome.tool_call_id == world.invocation_id
        assert row.status == ToolCallStatus.DENIED.value
        assert row.error_code == "tool_not_found"
        assert world.executions == []


async def test_a_re_entered_claim_whose_arguments_no_longer_validate_is_denied(
    tmp_path: Path,
) -> None:
    """The other pre-claim path, with the tool still registered.

    Its schema now refuses the arguments the claim was made with — a narrowed
    input model, or arguments the model regenerated differently on a retry.
    Nothing was dispatched either way, so the claim is closed as the denial
    rather than reported as somebody else's call.
    """
    async with _world(tmp_path, "revalidated") as world:
        await _abandon_claim_before_dispatch(world)
        assert (await world.row()).status == ToolCallStatus.CLAIMED.value

        async with world.sessions() as session:
            outcome = await world.gateway(session).request(
                "test.reentry",
                json.dumps({"label": "once", "unexpected": True}),
                invocation_id=world.invocation_id,
            )

        row = await world.row()
        assert outcome.status == "denied"
        assert outcome.error_code == "invalid_input"
        assert row.status == ToolCallStatus.DENIED.value
        assert row.error_code == "invalid_input"
        assert world.executions == []
