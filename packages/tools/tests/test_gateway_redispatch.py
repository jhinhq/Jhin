"""What recovery may do with a dispatch nobody can vouch for.

``test_gateway_claim_reentry`` covers the two states either side of the
dispatch compare-and-set. These are about the question that was never asked
once the row said ``executing``: could re-running this tool produce an effect
the first dispatch may already have produced?

A tool answers it in its definition, and the answer decides the ending:

* ``redispatch_is_safe=True`` — re-decide and run it again, bounded by the
  durable dispatch count, and if that runs out, a readable *failure* rather
  than a mystery;
* ``redispatch_is_safe=False`` — ``execution_unknown``, exactly as before.
  That half is the at-most-once guarantee and these tests exist to hold it
  in place while the other half changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import jhin_tools.gateway as gateway_module
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
from jhin_tools.gateway import (
    MAX_DISPATCH_ATTEMPTS,
    REDISPATCH_EXHAUSTED_CODE,
    GatewayOutcome,
    ToolGateway,
)
from jhin_tools.invocation import stable_tool_invocation_id

SAFE_TOOL = "test.redispatch_safe"
UNSAFE_TOOL = "test.redispatch_unsafe"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class _Output(BaseModel):
    executed: bool


Executor = Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]]


@dataclass
class _World:
    sessions: async_sessionmaker[AsyncSession]
    catalog: ToolCatalog
    workspace_id: UUID
    agent_id: UUID
    task_id: UUID
    run_id: UUID
    invocation_id: UUID
    executions: list[str] = field(default_factory=list)

    def gateway(self, session: AsyncSession) -> ToolGateway:
        return ToolGateway(
            ToolExecutionContext(
                session=session,
                workspace_id=self.workspace_id,
                task_id=self.task_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                agent_name="Redispatcher",
                session_factory=self.sessions,
            ),
            self.catalog,
        )

    async def request(self, gateway: ToolGateway, tool_name: str) -> GatewayOutcome:
        return await gateway.request(
            tool_name,
            json.dumps({"label": "once"}),
            invocation_id=self.invocation_id,
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

    async def revoke_grants(self) -> None:
        async with self.sessions() as session:
            grants = await session.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == self.agent_id,
                )
            )
            for grant in grants:
                await session.delete(grant)
            await session.commit()

    async def forge_dispatch_history(self, count: int) -> None:
        """Backdate the durable dispatch count to ``count`` attempts.

        The budget is read from the ``tool.call.dispatched`` trail rather
        than from process state, which is the only version of it that
        survives the worker dying — so this is how a test says "two workers
        have already tried this".
        """
        async with self.sessions() as session:
            for _ in range(count):
                session.add(
                    AuditEvent(
                        workspace_id=self.workspace_id,
                        actor_type="agent",
                        actor_id=self.agent_id,
                        action="tool.call.dispatched",
                        target_type="tool_call",
                        target_id=self.invocation_id,
                        metadata_json={"tool_name": SAFE_TOOL},
                    )
                )
            await session.commit()


def _definition(name: str, *, safe: bool) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Redispatch test",
        risk=RiskLevel.WRITE,
        input_model=_Input,
        output_model=_Output,
        required_capability=name,
        redispatch_is_safe=safe,
    )


@contextlib.asynccontextmanager
async def _world(
    tmp_path: Path,
    name: str,
    *,
    executor: Executor | None = None,
) -> AsyncIterator[_World]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ran: list[str] = []

    async def default_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        ran.append("executed")
        return _Output(executed=True)

    catalog = ToolCatalog()
    catalog.register(_definition(SAFE_TOOL, safe=True), executor or default_executor)
    catalog.register(_definition(UNSAFE_TOOL, safe=False), executor or default_executor)

    async with sessions() as setup:
        workspace = Workspace(name="Redispatch", slug=f"redispatch-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        agent = Agent(workspace_id=workspace.id, name="Redispatcher", slug="redispatcher")
        setup.add(agent)
        await setup.flush()
        task = Task(workspace_id=workspace.id, title="Redispatch", correlation_id=new_uuid7())
        setup.add(task)
        await setup.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.RUNNING.value,
        )
        setup.add(run)
        for capability in (SAFE_TOOL, UNSAFE_TOOL):
            setup.add(
                AgentCapabilityGrant(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    capability=capability,
                    scope_json={},
                    effect="allow",
                )
            )
        await setup.commit()
        identity = (workspace.id, agent.id, task.id, run.id)

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
    )
    await engine.dispose()


async def _abandon_inside_the_executor(world: _World, tool_name: str) -> None:
    """Leave the row exactly as a killed worker leaves it: dispatched, with
    no record of what the executor did."""
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
            await world.request(gateway, tool_name)
    # The state the incident left behind, and the premise of every test here.
    assert (await world.row()).status == ToolCallStatus.EXECUTING.value


async def test_a_dispatched_call_a_tool_says_is_safe_to_repeat_is_run_again(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, "safe") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)
        assert world.executions == []

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        finished = await world.row()
        assert (outcome.status, finished.status, world.executions) == (
            "executed",
            ToolCallStatus.COMPLETED.value,
            ["executed"],
        )
        actions = await world.audit_actions()
        assert actions.count("tool.call.execution_unknown") == 0
        assert actions.count("tool.call.dispatched") == 2
        [reentry] = await world.audit_metadata("tool.call.claim_reentered")
        assert reentry["code"] == "redispatch_after_unproven_outcome"
        assert reentry["dispatch_attempts"] == 1
        # The record names what the verdict rested on, not just the verdict.
        assert "outside the sandbox" in reentry["evidence"]


async def test_a_dispatched_call_a_tool_does_not_vouch_for_stays_unknown(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, "unsafe") as world:
        await _abandon_inside_the_executor(world, UNSAFE_TOOL)

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), UNSAFE_TOOL)

        finished = await world.row()
        assert (outcome.status, outcome.error_code, finished.status, world.executions) == (
            "execution_unknown",
            "execution_outcome_unknown",
            ToolCallStatus.EXECUTION_UNKNOWN.value,
            [],
        )
        actions = await world.audit_actions()
        assert actions.count("tool.call.dispatched") == 1
        assert actions.count("tool.call.claim_reentered") == 0


async def test_an_already_unknown_row_is_rescued_only_for_a_safe_tool(
    tmp_path: Path,
) -> None:
    """A row an earlier attempt already reconciled as unknown is the same
    question one step later, and gets the same answer."""
    async with _world(tmp_path, "already_unknown") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)
        async with world.sessions() as session:
            await session.execute(
                update(ToolCall)
                .where(ToolCall.id == world.invocation_id)
                .values(
                    status=ToolCallStatus.EXECUTION_UNKNOWN.value,
                    error_code="execution_outcome_unknown",
                )
            )
            await session.commit()

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        assert (outcome.status, world.executions) == ("executed", ["executed"])
        assert (await world.row()).status == ToolCallStatus.COMPLETED.value


async def test_the_dispatch_budget_ends_in_a_readable_failure_not_a_mystery(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, "budget") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)
        # One dispatch is already on the trail from the abandoned attempt.
        await world.forge_dispatch_history(MAX_DISPATCH_ATTEMPTS - 1)

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        finished = await world.row()
        assert (outcome.status, outcome.error_code, finished.status) == (
            "failed",
            REDISPATCH_EXHAUSTED_CODE,
            ToolCallStatus.FAILED.value,
        )
        # It did not run again, and it did not become somebody's manual chore.
        assert world.executions == []
        assert (await world.audit_actions()).count("tool.call.execution_unknown") == 0
        # The agent is told what to do with it, in a field it already reads.
        assert "Call the tool again" in finished.sanitized_output_json["hint"]


async def test_a_rescued_call_is_decided_again_rather_than_resumed(
    tmp_path: Path,
) -> None:
    """The re-run is a whole new decision. A grant taken away between the two
    attempts denies the second one — which is the difference between
    re-deciding a call and merely finishing it."""
    async with _world(tmp_path, "redecided") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)
        await world.revoke_grants()

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        assert (outcome.status, world.executions) == ("denied", [])
        assert (await world.row()).status == ToolCallStatus.DENIED.value


async def test_one_attempt_retries_its_own_unproven_outcome_for_a_safe_tool(
    tmp_path: Path,
) -> None:
    """The in-process half: an executor that fails without proving anything
    is retried inside the same request, so the agent gets the result."""
    attempts: list[str] = []

    async def flaky(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        attempts.append("call")
        if len(attempts) == 1:
            raise RuntimeError("the runner went away")
        return _Output(executed=True)

    async with _world(tmp_path, "flaky", executor=flaky) as world:
        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        assert (outcome.status, len(attempts)) == ("executed", 2)
        assert (await world.row()).status == ToolCallStatus.COMPLETED.value
        assert (await world.audit_actions()).count("tool.call.dispatched") == 2


async def test_one_attempt_does_not_retry_an_unproven_outcome_for_an_unsafe_tool(
    tmp_path: Path,
) -> None:
    attempts: list[str] = []

    async def flaky(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        attempts.append("call")
        raise RuntimeError("the runner went away")

    async with _world(tmp_path, "flaky_unsafe", executor=flaky) as world:
        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), UNSAFE_TOOL)

        assert (outcome.status, len(attempts)) == ("execution_unknown", 1)
        assert (await world.row()).status == ToolCallStatus.EXECUTION_UNKNOWN.value


@pytest.mark.parametrize("tool_name", [SAFE_TOOL, UNSAFE_TOOL])
async def test_an_undispatched_claim_is_untouched_by_the_classification(
    tmp_path: Path,
    tool_name: str,
) -> None:
    """A claim whose executor was never entered was always safe to finish,
    for every tool. Narrowing where ``execution_unknown`` applies must not
    quietly widen anything else."""
    async with _world(tmp_path, f"undispatched_{tool_name.rsplit('_', 1)[-1]}") as world:
        async with world.sessions() as session:
            gateway = world.gateway(session)

            async def die_before_dispatch(*args: Any, **kwargs: Any) -> GatewayOutcome:
                raise asyncio.CancelledError

            gateway._execute = die_before_dispatch  # type: ignore[method-assign]
            with pytest.raises(asyncio.CancelledError):
                await world.request(gateway, tool_name)
        assert (await world.row()).status == ToolCallStatus.CLAIMED.value

        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), tool_name)

        assert outcome.status == "executed"
        [reentry] = await world.audit_metadata("tool.call.claim_reentered")
        assert reentry["code"] == "claim_not_dispatched"


async def test_a_spent_dispatch_budget_ends_the_request_readably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline fix, from the outside: what one ``request`` returns when
    every dispatch it is allowed comes back unproven.

    It used to return ``execution_unknown`` — the operator's own sentence,
    "manual reconciliation is required", after three attempts at a tool that
    cannot reach anything outside Jhin. The loop stopped on the last
    *dispatch*, and the code that turns a spent budget into a readable failure
    only runs on the next way *in*, so the ending this whole change exists to
    produce was one entry further than the loop ever went.
    """
    attempts: list[str] = []

    async def never_answers(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        attempts.append("call")
        raise RuntimeError("the worker went away again")

    monkeypatch.setattr(gateway_module, "_REDISPATCH_BACKOFF_SECONDS", (0.0, 0.0))
    async with _world(tmp_path, "spent_budget", executor=never_answers) as world:
        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), SAFE_TOOL)

        row = await world.row()
        assert (outcome.status, outcome.error_code) == ("failed", REDISPATCH_EXHAUSTED_CODE)
        assert (row.status, row.error_code) == (
            ToolCallStatus.FAILED.value,
            REDISPATCH_EXHAUSTED_CODE,
        )
        # Every dispatch the budget allows was spent, and not one more.
        assert len(attempts) == MAX_DISPATCH_ATTEMPTS
        actions = await world.audit_actions()
        assert actions.count("tool.call.dispatched") == MAX_DISPATCH_ATTEMPTS
        # And the run is not asked to end: the agent gets a sentence it can act on.
        assert "Call the tool again" in row.sanitized_output_json["hint"]


async def test_an_unsafe_tool_still_ends_the_request_as_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same loop, unchanged. One dispatch, no retry,
    and the ending that stops the run — because a push that may have landed
    is exactly what nobody may guess about."""
    attempts: list[str] = []

    async def never_answers(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
        attempts.append("call")
        raise RuntimeError("the worker went away again")

    monkeypatch.setattr(gateway_module, "_REDISPATCH_BACKOFF_SECONDS", (0.0, 0.0))
    async with _world(tmp_path, "unsafe_budget", executor=never_answers) as world:
        async with world.sessions() as session:
            outcome = await world.request(world.gateway(session), UNSAFE_TOOL)

        assert (outcome.status, len(attempts)) == ("execution_unknown", 1)
        assert (await world.row()).status == ToolCallStatus.EXECUTION_UNKNOWN.value


def test_the_retry_loop_and_the_dispatch_budget_are_the_same_number() -> None:
    """A backoff added without a dispatch to spend it on, or the other way
    round, is how the two came apart in the first place."""
    assert len(gateway_module._REDISPATCH_BACKOFF_SECONDS) == MAX_DISPATCH_ATTEMPTS - 1
    assert gateway_module._MAX_INVOCATION_ENTRIES == MAX_DISPATCH_ATTEMPTS + 1


class _NarrowInput(BaseModel):
    """The same tool after its schema was narrowed: nothing validates now."""

    model_config = ConfigDict(extra="forbid")

    label: int


def _narrowed(name: str, *, safe: bool) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Redispatch test, after a schema change",
        risk=RiskLevel.WRITE,
        input_model=_NarrowInput,
        output_model=_Output,
        required_capability=name,
        redispatch_is_safe=safe,
    )


@pytest.mark.parametrize(
    ("safe", "expected_status", "expected_code"),
    [
        (True, "failed", REDISPATCH_EXHAUSTED_CODE),
        (False, "execution_unknown", "execution_outcome_unknown"),
    ],
)
async def test_the_doorway_that_cannot_revalidate_asks_the_same_question(
    tmp_path: Path,
    safe: bool,
    expected_status: str,
    expected_code: str,
) -> None:
    """The definition-independent re-entry, which used to answer on its own.

    A dispatched row reached after the tool's schema narrowed under it cannot
    be re-run — the stored arguments no longer validate, so there is no input
    to dispatch — and this doorway wrote ``execution_unknown`` without asking
    the tool anything. That made the reconciler's claim to own every such
    verdict untrue: the same row, reached through the ordinary door, would
    have been re-run or closed as a readable failure. It now asks, and the
    answer means what it means everywhere else — including the ``no`` that a
    tool nobody can look up any more still gets by default.
    """
    async with _world(tmp_path, f"unvalidated_{'safe' if safe else 'unsafe'}") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)

        narrowed = ToolCatalog()

        async def unreachable(ctx: ToolExecutionContext, payload: BaseModel) -> _Output:
            raise AssertionError("a call that cannot be validated must never be dispatched")

        narrowed.register(_narrowed(SAFE_TOOL, safe=safe), unreachable)

        async with world.sessions() as session:
            gateway = ToolGateway(
                ToolExecutionContext(
                    session=session,
                    workspace_id=world.workspace_id,
                    task_id=world.task_id,
                    run_id=world.run_id,
                    agent_id=world.agent_id,
                    agent_name="Redispatcher",
                    session_factory=world.sessions,
                ),
                narrowed,
            )
            outcome = await gateway.request(
                SAFE_TOOL,
                json.dumps({"label": "once"}),
                invocation_id=world.invocation_id,
            )

        row = await world.row()
        assert (outcome.status, outcome.error_code) == (expected_status, expected_code)
        assert row.error_code == expected_code
        assert world.executions == []


async def test_a_dispatched_row_whose_tool_is_gone_is_still_unknown(
    tmp_path: Path,
) -> None:
    """Recovery never guesses. An unregistered name cannot say a repeat is
    safe, so the doorway keeps the expensive answer for it."""
    async with _world(tmp_path, "unvalidated_revoked") as world:
        await _abandon_inside_the_executor(world, SAFE_TOOL)

        async with world.sessions() as session:
            gateway = ToolGateway(
                ToolExecutionContext(
                    session=session,
                    workspace_id=world.workspace_id,
                    task_id=world.task_id,
                    run_id=world.run_id,
                    agent_id=world.agent_id,
                    agent_name="Redispatcher",
                    session_factory=world.sessions,
                ),
                ToolCatalog(),
            )
            outcome = await gateway.request(
                SAFE_TOOL,
                json.dumps({"label": "once"}),
                invocation_id=world.invocation_id,
            )

        assert outcome.status == "execution_unknown"
        assert (await world.row()).status == ToolCallStatus.EXECUTION_UNKNOWN.value
