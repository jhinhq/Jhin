"""Real-PostgreSQL authorization and at-most-once regressions for Phase 9."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_agent_worker.activities import _cancel_pending_run_approvals
from jhin_api.approvals import service as approval_service
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import service as policy_service
from jhin_db import create_engine, create_session_factory
from jhin_db.migrate import upgrade_to_head
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    AuditEvent,
    Connection,
    Secret,
    Task,
    ToolCall,
    User,
    Workspace,
)
from jhin_domain import ActorType, ApprovalStatus, ToolCallStatus, WorkspaceRole, new_uuid7
from jhin_policy import ApprovalPreset, RiskLevel, ToolDefinition, rules_for_preset
from jhin_tools import stable_tool_invocation_id
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import GatewayOutcome, ToolGateway

pytestmark = pytest.mark.integration

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_USER = "jhin"
PG_PASSWORD = "jhin"
ADMIN_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/postgres"


@dataclass(frozen=True)
class PgDatabase:
    name: str
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class RuntimeIdentity:
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID
    agent_name: str


class RequestMeta(TypedDict):
    request_id: UUID
    ip_hash: str


class _EffectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class _EffectOutput(BaseModel):
    marker_id: str


class _ConnectionEffectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    label: str


class _PauseAfterDirectClaimGateway(ToolGateway):
    """Hold the winner in the durable-claim/executor-dispatch gap."""

    def __init__(
        self,
        context: ToolExecutionContext,
        catalog: ToolCatalog,
        *,
        claim_committed: asyncio.Event,
        release_claim: asyncio.Event,
    ) -> None:
        super().__init__(context, catalog)
        self._claim_committed = claim_committed
        self._release_claim = release_claim

    async def _claim_direct_call(
        self,
        definition: ToolDefinition,
        *,
        invocation_id: UUID,
        sanitized_input: dict[str, Any],
        dumped: dict[str, Any],
        connection_id: UUID | None,
    ) -> tuple[ToolCall | None, GatewayOutcome | None]:
        row, replay = await super()._claim_direct_call(
            definition,
            invocation_id=invocation_id,
            sanitized_input=sanitized_input,
            dumped=dumped,
            connection_id=connection_id,
        )
        if row is not None and replay is None:
            self._claim_committed.set()
            await self._release_claim.wait()
        return row, replay


class _PauseAfterParkedClaimGateway(ToolGateway):
    """Hold an approved winner after its parked call becomes executable."""

    def __init__(
        self,
        context: ToolExecutionContext,
        catalog: ToolCatalog,
        *,
        claim_committed: asyncio.Event,
        release_claim: asyncio.Event,
    ) -> None:
        super().__init__(context, catalog)
        self._claim_committed = claim_committed
        self._release_claim = release_claim

    async def _claim_parked_call(self, approval: Approval, row: ToolCall) -> GatewayOutcome | None:
        replay = await super()._claim_parked_call(approval, row)
        if replay is None:
            self._claim_committed.set()
            await self._release_claim.wait()
        return replay


class _CrashAfterDirectClaimGateway(ToolGateway):
    """Model process loss after the durable claim but before dispatch."""

    async def _claim_direct_call(
        self,
        definition: ToolDefinition,
        *,
        invocation_id: UUID,
        sanitized_input: dict[str, Any],
        dumped: dict[str, Any],
        connection_id: UUID | None,
    ) -> tuple[ToolCall | None, GatewayOutcome | None]:
        row, replay = await super()._claim_direct_call(
            definition,
            invocation_id=invocation_id,
            sanitized_input=sanitized_input,
            dumped=dumped,
            connection_id=connection_id,
        )
        if row is not None and replay is None:
            raise asyncio.CancelledError
        return row, replay


class _SignalHandle:
    def __init__(self) -> None:
        self.signals: list[tuple[str, list[str]]] = []

    async def signal(self, name: str, *, args: list[str]) -> None:
        self.signals.append((name, args))


class _TemporalClient:
    def __init__(self) -> None:
        self.handle = _SignalHandle()

    def get_workflow_handle(self, _workflow_id: str) -> _SignalHandle:
        return self.handle


@pytest.fixture
async def authorization_database() -> AsyncIterator[PgDatabase]:
    database_name = f"jhin_phase9_authorization_{uuid4().hex}"
    database_url = (
        f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{database_name}"
    )
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await admin.close()
    try:
        await asyncio.to_thread(upgrade_to_head, database_url)
        engine = create_engine(database_url)
        yield PgDatabase(
            name=database_name,
            engine=engine,
            sessions=create_session_factory(engine),
        )
        await engine.dispose()
    finally:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                database_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        finally:
            await admin.close()


async def _seed_runtime(
    database: PgDatabase,
    *,
    capability: str | None,
    approval_policy: list[dict[str, object]],
) -> tuple[RuntimeIdentity, WorkspaceContext]:
    async with database.sessions() as session:
        user = User(
            email=f"phase9-{new_uuid7().hex[:10]}@example.com",
            display_name="Phase 9 Admin",
            password_hash="x",
        )
        workspace = Workspace(
            name="Phase 9 Authorization",
            slug=f"phase9-authorization-{new_uuid7().hex[:10]}",
        )
        session.add_all([user, workspace])
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name="Authorization Agent",
            slug=f"authorization-agent-{new_uuid7().hex[:10]}",
            approval_policy_json=approval_policy,
        )
        session.add(agent)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            title="At-most-once authorization",
            assigned_agent_id=agent.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            task_id=task.id,
            agent_id=agent.id,
        )
        session.add(run)
        if capability is not None:
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
        return (
            RuntimeIdentity(
                workspace_id=workspace.id,
                task_id=task.id,
                run_id=run.id,
                agent_id=agent.id,
                agent_name=agent.name,
            ),
            WorkspaceContext(
                user=user,
                workspace_id=workspace.id,
                role=WorkspaceRole.ADMIN,
            ),
        )


def _runtime_context(
    database: PgDatabase,
    session: AsyncSession,
    identity: RuntimeIdentity,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        session=session,
        session_factory=database.sessions,
        workspace_id=identity.workspace_id,
        task_id=identity.task_id,
        run_id=identity.run_id,
        agent_id=identity.agent_id,
        agent_name=identity.agent_name,
    )


def _effect_catalog(
    *,
    tool_name: str,
    risk: RiskLevel,
    effect_action: str,
    executor_started: asyncio.Event,
    release_executor: asyncio.Event,
    execution_attempts: list[int],
) -> ToolCatalog:
    async def executor(ctx: ToolExecutionContext, payload: BaseModel) -> _EffectOutput:
        execution_attempts[0] += 1
        marker = AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action=effect_action,
            target_type="task",
            target_id=ctx.task_id,
            metadata_json={"label": payload.model_dump(mode="json")["label"]},
        )
        ctx.session.add(marker)
        await ctx.session.flush()
        executor_started.set()
        await release_executor.wait()
        return _EffectOutput(marker_id=str(marker.id))

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name=tool_name,
            description="A database-backed mutation used to verify at-most-once execution",
            risk=risk,
            input_model=_EffectInput,
            output_model=_EffectOutput,
            required_capability=tool_name,
            supports_approval=True,
        ),
        executor,
    )
    return catalog


async def _wait_for_lock_wait(database: PgDatabase) -> None:
    for _ in range(300):
        async with database.sessions() as observer:
            waiting = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND wait_event_type = 'Lock'"
                ),
                {"database_name": database.name},
            )
        if isinstance(waiting, int) and waiting > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the competing PostgreSQL session did not enter a lock wait")


async def _race_direct_execution(
    database: PgDatabase,
    identity: RuntimeIdentity,
    *,
    catalog: ToolCatalog,
    tool_name: str,
    invocation_id: UUID,
    executor_started: asyncio.Event,
    release_executor: asyncio.Event,
) -> list[GatewayOutcome]:
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()
    async with database.sessions() as first_session, database.sessions() as second_session:
        first = asyncio.create_task(
            _PauseAfterDirectClaimGateway(
                _runtime_context(database, first_session, identity),
                catalog,
                claim_committed=claim_committed,
                release_claim=release_claim,
            ).request(
                tool_name,
                '{"label": "one production effect"}',
                provider_call_id="provider-first",
                invocation_id=invocation_id,
            )
        )
        second: asyncio.Task[GatewayOutcome] | None = None
        try:
            await asyncio.wait_for(claim_committed.wait(), timeout=5)
            second = asyncio.create_task(
                ToolGateway(_runtime_context(database, second_session, identity), catalog).request(
                    tool_name,
                    '{"label": "one production effect"}',
                    provider_call_id="provider-regenerated-on-retry",
                    invocation_id=invocation_id,
                )
            )
            await _wait_for_lock_wait(database)
            release_claim.set()
            await asyncio.wait_for(executor_started.wait(), timeout=5)
            release_executor.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), timeout=10
            )
        finally:
            release_claim.set()
            release_executor.set()
            tasks = [first, *([second] if second is not None else [])]
            await asyncio.gather(*tasks, return_exceptions=True)
        await first_session.rollback()
        await second_session.rollback()

    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [result for result in results if isinstance(result, GatewayOutcome)]


async def _assert_one_effect_and_terminal_replay(
    database: PgDatabase,
    identity: RuntimeIdentity,
    *,
    catalog: ToolCatalog,
    tool_name: str,
    effect_action: str,
    invocation_id: UUID,
    race_results: list[GatewayOutcome],
    execution_attempts: list[int],
) -> None:
    assert len(race_results) == 2
    assert [result.status for result in race_results] == ["executed", "executed"]
    assert {result.tool_call_id for result in race_results} == {invocation_id}
    assert sum(result.replayed for result in race_results) == 1
    assert execution_attempts == [1]

    async with database.sessions() as verification:
        effect_count = await verification.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == effect_action)
        )
        claim_count = await verification.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "tool.call.claimed",
                AuditEvent.target_id == invocation_id,
            )
        )
        tool_call_count = await verification.scalar(
            select(func.count(ToolCall.id)).where(ToolCall.id == invocation_id)
        )
        row = await verification.get(ToolCall, invocation_id)
        assert row is not None
        assert row.status == ToolCallStatus.COMPLETED.value
        assert effect_count == claim_count == tool_call_count == 1

        replay = await ToolGateway(
            _runtime_context(database, verification, identity), catalog
        ).request(
            tool_name,
            '{"label": "one production effect"}',
            provider_call_id="provider-regenerated-again",
            invocation_id=invocation_id,
        )
        assert replay.status == "executed"
        assert replay.replayed is True
        assert replay.tool_call_id == invocation_id
        assert execution_attempts == [1]


async def test_autonomous_elevated_invocation_race_executes_once_and_replays(
    authorization_database: PgDatabase,
) -> None:
    tool_name = "test.phase9.autonomous_elevated"
    effect_action = "test.phase9.autonomous_elevated.effect"
    autonomous_rules = [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.AUTONOMOUS)
    ]
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=autonomous_rules,
    )
    executor_started = asyncio.Event()
    release_executor = asyncio.Event()
    execution_attempts = [0]
    catalog = _effect_catalog(
        tool_name=tool_name,
        risk=RiskLevel.ELEVATED,
        effect_action=effect_action,
        executor_started=executor_started,
        release_executor=release_executor,
        execution_attempts=execution_attempts,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=1, tool_call_ordinal=0)

    race_results = await _race_direct_execution(
        authorization_database,
        identity,
        catalog=catalog,
        tool_name=tool_name,
        invocation_id=invocation_id,
        executor_started=executor_started,
        release_executor=release_executor,
    )

    await _assert_one_effect_and_terminal_replay(
        authorization_database,
        identity,
        catalog=catalog,
        tool_name=tool_name,
        effect_action=effect_action,
        invocation_id=invocation_id,
        race_results=race_results,
        execution_attempts=execution_attempts,
    )


async def test_custom_auto_destructive_invocation_race_executes_once_and_replays(
    authorization_database: PgDatabase,
) -> None:
    tool_name = "test.phase9.custom_auto_destructive"
    effect_action = "test.phase9.custom_auto_destructive.effect"
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=[
            {
                "capability": tool_name,
                "risk": RiskLevel.DESTRUCTIVE.value,
                "action": "auto",
            }
        ],
    )
    executor_started = asyncio.Event()
    release_executor = asyncio.Event()
    execution_attempts = [0]
    catalog = _effect_catalog(
        tool_name=tool_name,
        risk=RiskLevel.DESTRUCTIVE,
        effect_action=effect_action,
        executor_started=executor_started,
        release_executor=release_executor,
        execution_attempts=execution_attempts,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=2, tool_call_ordinal=0)

    race_results = await _race_direct_execution(
        authorization_database,
        identity,
        catalog=catalog,
        tool_name=tool_name,
        invocation_id=invocation_id,
        executor_started=executor_started,
        release_executor=release_executor,
    )

    await _assert_one_effect_and_terminal_replay(
        authorization_database,
        identity,
        catalog=catalog,
        tool_name=tool_name,
        effect_action=effect_action,
        invocation_id=invocation_id,
        race_results=race_results,
        execution_attempts=execution_attempts,
    )


async def test_interrupted_auto_invocation_is_unknown_and_never_reexecutes(
    authorization_database: PgDatabase,
) -> None:
    tool_name = "test.phase9.interrupted_effect"
    effect_action = "test.phase9.interrupted_effect.external"
    autonomous_rules = [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.AUTONOMOUS)
    ]
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=autonomous_rules,
    )
    execution_attempts = 0

    async def interrupted_executor(ctx: ToolExecutionContext, payload: BaseModel) -> _EffectOutput:
        nonlocal execution_attempts
        execution_attempts += 1
        # Commit through an independent session to model an external provider
        # accepting the mutation before this worker is cancelled.
        async with authorization_database.sessions() as external_effect_session:
            external_effect_session.add(
                AuditEvent(
                    workspace_id=ctx.workspace_id,
                    actor_type=ActorType.AGENT.value,
                    actor_id=ctx.agent_id,
                    action=effect_action,
                    target_type="task",
                    target_id=ctx.task_id,
                    metadata_json={"label": payload.model_dump(mode="json")["label"]},
                )
            )
            await external_effect_session.commit()
        raise asyncio.CancelledError

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name=tool_name,
            description="An interrupted production mutation",
            risk=RiskLevel.ELEVATED,
            input_model=_EffectInput,
            output_model=_EffectOutput,
            required_capability=tool_name,
            supports_approval=True,
        ),
        interrupted_executor,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=3, tool_call_ordinal=0)

    async with authorization_database.sessions() as first_session:
        with pytest.raises(asyncio.CancelledError):
            await ToolGateway(
                _runtime_context(authorization_database, first_session, identity), catalog
            ).request(
                tool_name,
                '{"label": "provider accepted before cancellation"}',
                invocation_id=invocation_id,
            )
        await first_session.rollback()

    async with authorization_database.sessions() as retry_session:
        retry = await ToolGateway(
            _runtime_context(authorization_database, retry_session, identity), catalog
        ).request(
            tool_name,
            '{"label": "provider accepted before cancellation"}',
            provider_call_id="provider-regenerated-after-cancellation",
            invocation_id=invocation_id,
        )
        assert retry.status == "execution_unknown"
        assert retry.replayed is True
        assert retry.error_code == "execution_outcome_unknown"

        row = await retry_session.get(ToolCall, invocation_id)
        assert row is not None
        assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
        effect_count = await retry_session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == effect_action)
        )
        claim_count = await retry_session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "tool.call.claimed",
                AuditEvent.target_id == invocation_id,
            )
        )
        assert effect_count == claim_count == execution_attempts == 1


async def test_crash_after_direct_claim_before_dispatch_is_unknown_without_effect(
    authorization_database: PgDatabase,
) -> None:
    tool_name = "test.phase9.claimed_before_dispatch"
    effect_action = "test.phase9.claimed_before_dispatch.effect"
    autonomous_rules = [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.AUTONOMOUS)
    ]
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=autonomous_rules,
    )
    executor_started = asyncio.Event()
    release_executor = asyncio.Event()
    release_executor.set()
    execution_attempts = [0]
    catalog = _effect_catalog(
        tool_name=tool_name,
        risk=RiskLevel.ELEVATED,
        effect_action=effect_action,
        executor_started=executor_started,
        release_executor=release_executor,
        execution_attempts=execution_attempts,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=5, tool_call_ordinal=0)

    async with authorization_database.sessions() as crashed_session:
        with pytest.raises(asyncio.CancelledError):
            await _CrashAfterDirectClaimGateway(
                _runtime_context(authorization_database, crashed_session, identity), catalog
            ).request(
                tool_name,
                '{"label":"must-not-dispatch"}',
                invocation_id=invocation_id,
            )
        await crashed_session.rollback()

    async with authorization_database.sessions() as retry_session:
        retry = await ToolGateway(
            _runtime_context(authorization_database, retry_session, identity), catalog
        ).request(
            tool_name,
            '{"label":"must-not-dispatch"}',
            provider_call_id="provider-after-worker-loss",
            invocation_id=invocation_id,
        )
        assert retry.status == "execution_unknown"
        assert retry.error_code == "execution_outcome_unknown"
        assert retry.replayed is False

        row = await retry_session.get(ToolCall, invocation_id)
        assert row is not None
        assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
        assert row.completed_at is not None
        assert (
            await retry_session.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == effect_action)
            )
            == 0
        )
        assert (
            await retry_session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "tool.call.claimed",
                    AuditEvent.target_id == invocation_id,
                )
            )
            == 1
        )
        assert (
            await retry_session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "tool.call.execution_unknown",
                    AuditEvent.target_id == invocation_id,
                )
            )
            == 1
        )
    assert execution_attempts == [0]
    assert executor_started.is_set() is False


async def test_approved_invocation_race_executes_once_and_replays(
    authorization_database: PgDatabase,
) -> None:
    tool_name = "test.phase9.approved_effect"
    effect_action = "test.phase9.approved_effect.effect"
    balanced_rules = [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)
    ]
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=balanced_rules,
    )
    executor_started = asyncio.Event()
    release_executor = asyncio.Event()
    execution_attempts = [0]
    catalog = _effect_catalog(
        tool_name=tool_name,
        risk=RiskLevel.ELEVATED,
        effect_action=effect_action,
        executor_started=executor_started,
        release_executor=release_executor,
        execution_attempts=execution_attempts,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=4, tool_call_ordinal=0)

    async with authorization_database.sessions() as staging_session:
        parked = await ToolGateway(
            _runtime_context(authorization_database, staging_session, identity), catalog
        ).request(
            tool_name,
            '{"label": "one approved production effect"}',
            provider_call_id="provider-approval",
            invocation_id=invocation_id,
        )
        assert parked.status == "needs_approval"
        assert parked.approval_id is not None
        approval = await staging_session.get(Approval, parked.approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.APPROVED.value
        approval.decided_at = datetime.now(UTC)
        await staging_session.commit()
        approval_id = parked.approval_id

    async with (
        authorization_database.sessions() as first_session,
        authorization_database.sessions() as second_session,
    ):
        claim_committed = asyncio.Event()
        release_claim = asyncio.Event()
        first = asyncio.create_task(
            _PauseAfterParkedClaimGateway(
                _runtime_context(authorization_database, first_session, identity),
                catalog,
                claim_committed=claim_committed,
                release_claim=release_claim,
            ).resolve_approved(approval_id)
        )
        second: asyncio.Task[GatewayOutcome] | None = None
        try:
            await asyncio.wait_for(claim_committed.wait(), timeout=5)
            second = asyncio.create_task(
                ToolGateway(
                    _runtime_context(authorization_database, second_session, identity),
                    catalog,
                ).resolve_approved(approval_id)
            )
            await _wait_for_lock_wait(authorization_database)
            release_claim.set()
            await asyncio.wait_for(executor_started.wait(), timeout=5)
            release_executor.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), timeout=10
            )
        finally:
            release_claim.set()
            release_executor.set()
            tasks = [first, *([second] if second is not None else [])]
            await asyncio.gather(*tasks, return_exceptions=True)
        await first_session.rollback()
        await second_session.rollback()

    for result in results:
        if isinstance(result, BaseException):
            raise result
    outcomes = [result for result in results if isinstance(result, GatewayOutcome)]
    assert len(outcomes) == 2
    assert [outcome.status for outcome in outcomes] == ["executed", "executed"]
    assert {outcome.tool_call_id for outcome in outcomes} == {invocation_id}
    assert sum(outcome.replayed for outcome in outcomes) == 1
    assert execution_attempts == [1]

    async with authorization_database.sessions() as verification:
        effect_count = await verification.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == effect_action)
        )
        claim_count = await verification.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "tool.call.claimed",
                AuditEvent.target_id == invocation_id,
            )
        )
        row = await verification.get(ToolCall, invocation_id)
        assert row is not None
        assert row.status == ToolCallStatus.COMPLETED.value
        assert effect_count == claim_count == 1

        replay = await ToolGateway(
            _runtime_context(authorization_database, verification, identity), catalog
        ).resolve_approved(approval_id)
        assert replay.status == "executed"
        assert replay.replayed is True
        assert execution_attempts == [1]


@pytest.mark.parametrize("drift", ["credential", "config", "status", "auth_type"])
async def test_approved_connection_drift_after_claim_is_refreshed_before_dispatch(
    authorization_database: PgDatabase,
    drift: str,
) -> None:
    tool_name = "test.phase9.approved_connection_effect"
    effect_action = "test.phase9.approved_connection_effect.effect"
    balanced_rules = [
        rule.model_dump(mode="json") for rule in rules_for_preset(ApprovalPreset.BALANCED)
    ]
    identity, _ctx = await _seed_runtime(
        authorization_database,
        capability=tool_name,
        approval_policy=balanced_rules,
    )
    async with authorization_database.sessions() as setup:
        secret = Secret(
            workspace_id=identity.workspace_id,
            name=f"phase9-connection-secret-{new_uuid7().hex[:8]}",
            type="connection_credentials",
            ciphertext=b"ciphertext",
            nonce=b"nonce",
            wrapped_data_key=b"wrapped-key",
            key_version=1,
            secret_fingerprint="a" * 64,
            masked_hint="••••TEST",
        )
        setup.add(secret)
        await setup.flush()
        connection = Connection(
            workspace_id=identity.workspace_id,
            connector_type="test",
            name=f"Phase 9 connection {new_uuid7().hex[:8]}",
            auth_type="api_key",
            status="active",
            encrypted_secret_id=secret.id,
            config_json={"project": "original"},
        )
        setup.add(connection)
        await setup.commit()
        connection_id = connection.id
        secret_id = secret.id

    execution_attempts = 0

    async def executor(ctx: ToolExecutionContext, payload: BaseModel) -> _EffectOutput:
        nonlocal execution_attempts
        execution_attempts += 1
        marker = AuditEvent(
            workspace_id=ctx.workspace_id,
            actor_type=ActorType.AGENT.value,
            actor_id=ctx.agent_id,
            action=effect_action,
            target_type="task",
            target_id=ctx.task_id,
            metadata_json={"label": payload.model_dump(mode="json")["label"]},
        )
        ctx.session.add(marker)
        await ctx.session.flush()
        return _EffectOutput(marker_id=str(marker.id))

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name=tool_name,
            description="Approval connection TOCTOU regression",
            risk=RiskLevel.ELEVATED,
            input_model=_ConnectionEffectInput,
            output_model=_EffectOutput,
            required_capability=tool_name,
            supports_approval=True,
            scope_keys=("connection_id",),
        ),
        executor,
    )
    invocation_id = stable_tool_invocation_id(identity.run_id, step_index=6, tool_call_ordinal=0)
    arguments_json = f'{{"connection_id":"{connection_id}","label":"must-not-dispatch"}}'
    async with authorization_database.sessions() as staging_session:
        parked = await ToolGateway(
            _runtime_context(authorization_database, staging_session, identity), catalog
        ).request(
            tool_name,
            arguments_json,
            invocation_id=invocation_id,
        )
        assert parked.status == "needs_approval"
        assert parked.approval_id is not None
        approval = await staging_session.get(Approval, parked.approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.APPROVED.value
        approval.decided_at = datetime.now(UTC)
        await staging_session.commit()
        approval_id = parked.approval_id

    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()
    async with authorization_database.sessions() as resolver_session:
        resolver = asyncio.create_task(
            _PauseAfterParkedClaimGateway(
                _runtime_context(authorization_database, resolver_session, identity),
                catalog,
                claim_committed=claim_committed,
                release_claim=release_claim,
            ).resolve_approved(approval_id)
        )
        try:
            await asyncio.wait_for(claim_committed.wait(), timeout=5)
            async with authorization_database.sessions() as mutator:
                if drift == "credential":
                    current_secret = await mutator.get(Secret, secret_id)
                    assert current_secret is not None
                    current_secret.secret_fingerprint = "b" * 64
                    current_secret.key_version += 1
                else:
                    current_connection = await mutator.get(Connection, connection_id)
                    assert current_connection is not None
                    if drift == "config":
                        current_connection.config_json = {"project": "changed"}
                    elif drift == "status":
                        current_connection.status = "disabled"
                    else:
                        current_connection.auth_type = "oauth"
                await mutator.commit()
            release_claim.set()
            outcome = await asyncio.wait_for(resolver, timeout=10)
        finally:
            release_claim.set()
            await asyncio.gather(resolver, return_exceptions=True)
        await resolver_session.rollback()

    assert outcome.status == "denied"
    assert outcome.decision_code == "approval_connection_changed"
    assert execution_attempts == 0
    async with authorization_database.sessions() as verification:
        row = await verification.get(ToolCall, invocation_id)
        assert row is not None
        assert row.status == ToolCallStatus.DENIED.value
        assert (
            await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == effect_action)
            )
            == 0
        )


async def _backend_pid(session: AsyncSession) -> int:
    pid = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


async def _wait_for_backend_lock(database: PgDatabase, pid: int) -> None:
    for _ in range(300):
        async with database.sessions() as observer:
            wait_type = await observer.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if wait_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"backend {pid} did not enter a PostgreSQL lock wait")


def _request_meta() -> RequestMeta:
    return {"request_id": new_uuid7(), "ip_hash": "phase9-authorization-race"}


async def test_exact_duplicate_scoped_grant_race_returns_one_conflict(
    authorization_database: PgDatabase,
) -> None:
    identity, ctx = await _seed_runtime(
        authorization_database,
        capability=None,
        approval_policy=[],
    )
    scope = {"connection_id": str(new_uuid7())}
    capability = "test.phase9.scoped_mutation"

    async with (
        authorization_database.sessions() as blocker,
        authorization_database.sessions() as first_session,
        authorization_database.sessions() as second_session,
    ):
        await blocker.execute(
            select(Agent)
            .where(
                Agent.id == identity.agent_id,
                Agent.workspace_id == identity.workspace_id,
            )
            .with_for_update()
        )
        first_pid = await _backend_pid(first_session)
        second_pid = await _backend_pid(second_session)

        async def create(session: AsyncSession) -> AgentCapabilityGrant:
            return await policy_service.create_grant(
                session,
                ctx,
                identity.agent_id,
                capability=capability,
                scope=scope,
                effect="allow",
                **_request_meta(),
            )

        first = asyncio.create_task(create(first_session))
        second = asyncio.create_task(create(second_session))
        await asyncio.gather(
            _wait_for_backend_lock(authorization_database, first_pid),
            _wait_for_backend_lock(authorization_database, second_pid),
        )
        await blocker.commit()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=10
        )
        await first_session.rollback()
        await second_session.rollback()

    successes = [result for result in results if isinstance(result, AgentCapabilityGrant)]
    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, HTTPException):
            raise result
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with authorization_database.sessions() as verification:
        grants = list(
            await verification.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.workspace_id == identity.workspace_id,
                    AgentCapabilityGrant.agent_id == identity.agent_id,
                    AgentCapabilityGrant.capability == capability,
                    AgentCapabilityGrant.effect == "allow",
                )
            )
        )
    assert len(grants) == 1
    assert grants[0].scope_json == scope


async def test_approval_decision_race_has_one_durable_winner_and_signal(
    authorization_database: PgDatabase,
) -> None:
    identity, ctx = await _seed_runtime(
        authorization_database,
        capability=None,
        approval_policy=[],
    )
    async with authorization_database.sessions() as setup:
        task = await setup.get(Task, identity.task_id)
        assert task is not None
        task.temporal_workflow_id = "phase9-approval-decision-race"
        approval = Approval(
            workspace_id=identity.workspace_id,
            task_id=identity.task_id,
            run_id=identity.run_id,
            requested_by_agent_id=identity.agent_id,
            action_type="test.phase9.approval_decision_race",
            action_payload_sanitized={},
            reason="race the human decision",
            status=ApprovalStatus.PENDING.value,
            requested_at=datetime.now(UTC),
        )
        setup.add(approval)
        await setup.commit()
        approval_id = approval.id

    temporal = _TemporalClient()
    async with (
        authorization_database.sessions() as blocker,
        authorization_database.sessions() as first_session,
        authorization_database.sessions() as second_session,
    ):
        await blocker.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
        first_pid = await _backend_pid(first_session)
        second_pid = await _backend_pid(second_session)

        async def decide(session: AsyncSession, decision: str) -> str:
            decided = await approval_service.decide(
                session,
                ctx,
                temporal,  # type: ignore[arg-type]
                approval_id,
                decision=decision,
                **_request_meta(),
            )
            return decided.status

        first = asyncio.create_task(decide(first_session, ApprovalStatus.APPROVED.value))
        second = asyncio.create_task(decide(second_session, ApprovalStatus.REJECTED.value))
        await asyncio.gather(
            _wait_for_backend_lock(authorization_database, first_pid),
            _wait_for_backend_lock(authorization_database, second_pid),
        )
        await blocker.commit()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=10
        )
        await first_session.rollback()
        await second_session.rollback()

    successes = [result for result in results if isinstance(result, str)]
    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, HTTPException):
            raise result
    assert len(successes) == 1
    assert len(conflicts) == 1
    winning_status = successes[0]
    assert winning_status in {
        ApprovalStatus.APPROVED.value,
        ApprovalStatus.REJECTED.value,
    }
    assert temporal.handle.signals == [("approval_decision", [str(approval_id), winning_status])]

    async with authorization_database.sessions() as verification:
        persisted = await verification.get(Approval, approval_id)
        assert persisted is not None
        assert persisted.status == winning_status
        assert persisted.decided_by_user_id == ctx.user.id
        decisions = list(
            await verification.scalars(
                select(AuditEvent).where(
                    AuditEvent.target_id == approval_id,
                    AuditEvent.action.in_(("approval.approved", "approval.rejected")),
                )
            )
        )
        assert len(decisions) == 1
        assert decisions[0].action == f"approval.{winning_status}"


async def test_approval_and_run_finalizer_race_preserves_one_coherent_winner(
    authorization_database: PgDatabase,
) -> None:
    identity, ctx = await _seed_runtime(
        authorization_database,
        capability=None,
        approval_policy=[],
    )
    async with authorization_database.sessions() as setup:
        task = await setup.get(Task, identity.task_id)
        assert task is not None
        task.temporal_workflow_id = "phase9-approval-finalizer-race"
        approval = Approval(
            workspace_id=identity.workspace_id,
            task_id=identity.task_id,
            run_id=identity.run_id,
            requested_by_agent_id=identity.agent_id,
            action_type="test.phase9.approval_finalizer_race",
            action_payload_sanitized={},
            reason="race approval against finalization",
            status=ApprovalStatus.PENDING.value,
            requested_at=datetime.now(UTC),
        )
        setup.add(approval)
        await setup.flush()
        tool_call = ToolCall(
            workspace_id=identity.workspace_id,
            run_id=identity.run_id,
            agent_id=identity.agent_id,
            tool_name=approval.action_type,
            sanitized_input_json={"label": "pending"},
            sanitized_output_json={},
            status=ToolCallStatus.PENDING_APPROVAL.value,
            approval_id=approval.id,
            started_at=datetime.now(UTC),
        )
        setup.add(tool_call)
        await setup.commit()
        approval_id = approval.id
        tool_call_id = tool_call.id

    temporal = _TemporalClient()
    async with (
        authorization_database.sessions() as blocker,
        authorization_database.sessions() as decision_session,
        authorization_database.sessions() as finalizer_session,
    ):
        await blocker.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
        decision_pid = await _backend_pid(decision_session)
        finalizer_pid = await _backend_pid(finalizer_session)

        async def approve() -> str:
            decided = await approval_service.decide(
                decision_session,
                ctx,
                temporal,  # type: ignore[arg-type]
                approval_id,
                decision=ApprovalStatus.APPROVED.value,
                **_request_meta(),
            )
            return decided.status

        async def finalize() -> int:
            cancelled = await _cancel_pending_run_approvals(
                finalizer_session,
                workspace_id=identity.workspace_id,
                run_id=identity.run_id,
            )
            await finalizer_session.commit()
            return cancelled

        decision_task = asyncio.create_task(approve())
        finalizer_task = asyncio.create_task(finalize())
        await asyncio.gather(
            _wait_for_backend_lock(authorization_database, decision_pid),
            _wait_for_backend_lock(authorization_database, finalizer_pid),
        )
        await blocker.commit()
        decision_result, finalizer_result = await asyncio.wait_for(
            asyncio.gather(
                decision_task,
                finalizer_task,
                return_exceptions=True,
            ),
            timeout=10,
        )
        await decision_session.rollback()
        await finalizer_session.rollback()

    if isinstance(decision_result, BaseException) and not isinstance(
        decision_result, HTTPException
    ):
        raise decision_result
    assert isinstance(finalizer_result, int)
    async with authorization_database.sessions() as verification:
        persisted_approval = await verification.get(Approval, approval_id)
        persisted_call = await verification.get(ToolCall, tool_call_id)
        assert persisted_approval is not None
        assert persisted_call is not None
        approved_audits = await verification.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.target_id == approval_id,
                AuditEvent.action == "approval.approved",
            )
        )

    if decision_result == ApprovalStatus.APPROVED.value:
        assert finalizer_result == 0
        assert persisted_approval.status == ApprovalStatus.APPROVED.value
        assert persisted_call.status == ToolCallStatus.PENDING_APPROVAL.value
        assert approved_audits == 1
        assert temporal.handle.signals == [
            (
                "approval_decision",
                [str(approval_id), ApprovalStatus.APPROVED.value],
            )
        ]
    else:
        assert isinstance(decision_result, HTTPException)
        assert decision_result.status_code == 409
        assert finalizer_result == 1
        assert persisted_approval.status == ApprovalStatus.CANCELLED.value
        assert persisted_call.status == ToolCallStatus.REJECTED.value
        assert persisted_call.error_code == "run_ended"
        assert approved_audits == 0
        assert temporal.handle.signals == []
