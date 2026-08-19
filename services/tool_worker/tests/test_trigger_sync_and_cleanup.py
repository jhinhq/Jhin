"""Trigger comment-back claims and idempotent sandbox cleanup."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_connectors.linear.schemas import CommentCreateInput, CommentCreateOutput
from jhin_db import create_engine as create_database_engine
from jhin_db import create_session_factory
from jhin_db.base import Base
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AuditEvent,
    Connection,
    RunEvent,
    Secret,
    Task,
    ToolCall,
    Trigger,
    Workspace,
)
from jhin_domain import (
    ConnectionStatus,
    RunStatus,
    SecretType,
    TaskState,
    ToolCallStatus,
    new_uuid7,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tool_worker.cleanup_activities import CleanupActivities
from jhin_tool_worker.trigger_activities import TriggerToolActivities
from jhin_tools import ToolCatalog, ToolExecutionContext, stable_sync_invocation_id
from jhin_workflows import TOOL_TASK_QUEUE
from jhin_workflows.agent_task.shared import (
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
)
from jhin_workflows.tool_compat import (
    CleanupCompatibilityWorkflow,
    SyncExternalToolInput,
)
from jhin_workflows.triggered_task.shared import SyncExternalResult

WORKSPACE_ID = "018f4d52-8b93-7d41-8ac7-7f190f092001"
RUN_ID = "018f4d52-8b93-7d41-8ac7-7f190f092002"


@dataclass
class _Publisher:
    events: list[Any] = field(default_factory=list)

    async def publish(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _Resources:
    session_factory: async_sessionmaker[AsyncSession]
    publisher: _Publisher
    crypto: None = None
    test_barrier: None = None


@dataclass
class SyncWorld:
    activities: TriggerToolActivities
    sessions: async_sessionmaker[AsyncSession]
    resources: _Resources
    workspace: Workspace
    agent: Agent
    task: Task
    run: AgentRun
    trigger: Trigger
    connection: Connection
    effect_bodies: list[str]
    fail_after_effect: list[bool]

    @property
    def ids_only_params(self) -> SyncExternalToolInput:
        return SyncExternalToolInput(
            workspace_id=str(self.workspace.id),
            task_id=str(self.task.id),
            run_id=str(self.run.id),
        )

    @property
    def expected_body(self) -> str:
        return (
            "**Jhin** — trigger “Linear sync”: the assigned agent completed the task. "
            f"Task `{self.task.id}` (completed)."
        )

    async def tool_call(self) -> ToolCall | None:
        async with self.sessions() as session:
            return await session.get(ToolCall, stable_sync_invocation_id(self.run.id))

    async def count_events(self, event_type: str) -> int:
        async with self.sessions() as session:
            value = await session.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == self.run.id,
                    RunEvent.event_type == event_type,
                )
            )
            return int(value or 0)

    async def count_audits(self, action: str) -> int:
        async with self.sessions() as session:
            value = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.workspace_id == self.workspace.id,
                    AuditEvent.action == action,
                )
            )
            return int(value or 0)

    async def event_payload(self, event_type: str) -> dict[str, Any]:
        async with self.sessions() as session:
            event = await session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == self.run.id,
                    RunEvent.event_type == event_type,
                )
            )
            assert event is not None
            return event.payload_json

    async def latest_audit_metadata(self, action: str) -> dict[str, Any]:
        async with self.sessions() as session:
            audit = await session.scalar(
                select(AuditEvent)
                .where(
                    AuditEvent.workspace_id == self.workspace.id,
                    AuditEvent.action == action,
                )
                .order_by(AuditEvent.created_at.desc())
            )
            assert audit is not None
            return audit.metadata_json

    async def seed_executing_claim(self) -> None:
        async with self.sessions() as session:
            session.add(
                ToolCall(
                    id=stable_sync_invocation_id(self.run.id),
                    workspace_id=self.workspace.id,
                    run_id=self.run.id,
                    agent_id=self.agent.id,
                    tool_name="system.trigger.sync_external",
                    connection_id=self.connection.id,
                    sanitized_input_json={
                        "connection_id": str(self.connection.id),
                        "issue": "ENG-77",
                        "body": self.expected_body,
                    },
                    sanitized_output_json={},
                    status=ToolCallStatus.EXECUTING.value,
                    started_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def mutate_authority(self, case: str) -> None:
        async with self.sessions() as session:
            if case == "trigger_disabled":
                trigger = await session.get(Trigger, self.trigger.id)
                assert trigger is not None
                trigger.enabled = False
            elif case == "comment_back_disabled":
                trigger = await session.get(Trigger, self.trigger.id)
                assert trigger is not None
                trigger.action_config_json = {"comment_back": False}
            elif case == "connection_disabled":
                connection = await session.get(Connection, self.connection.id)
                assert connection is not None
                connection.status = ConnectionStatus.DISABLED.value
            elif case == "trigger_renamed":
                trigger = await session.get(Trigger, self.trigger.id)
                assert trigger is not None
                trigger.name = "Renamed after sync"
            elif case == "external_id_drift":
                task = await session.get(Task, self.task.id)
                assert task is not None
                task.external_id = "ENG-999"
            elif case == "revoked_and_drifted":
                trigger = await session.get(Trigger, self.trigger.id)
                task = await session.get(Task, self.task.id)
                assert trigger is not None
                assert task is not None
                trigger.enabled = False
                trigger.name = "Revoked after claim"
                task.external_id = "ENG-999"
            else:
                raise AssertionError(f"unknown authority case {case}")
            await session.commit()

    async def configure_engineering_run(
        self,
        status: str,
        *,
        valid_parent: bool = True,
        child_run: bool = True,
    ) -> UUID:
        async with self.sessions() as session:
            parent = await session.get(Task, self.task.id)
            run = await session.get(AgentRun, self.run.id)
            assert parent is not None
            assert run is not None
            parent.state = (
                TaskState.COMPLETED.value if status == "completed" else TaskState.FAILED.value
            )
            parent.metadata_json = {
                **parent.metadata_json,
                "engineering_result": {
                    "status": status,
                    "verdict": "pass" if status == "completed" else "fail",
                    "cycles_used": 1,
                },
            }
            if not child_run:
                run.status = (
                    RunStatus.FAILED.value
                    if status == "implementation_failed"
                    else RunStatus.COMPLETED.value
                )
                await session.commit()
                return parent.id
            child_parent_id = parent.id
            if not valid_parent:
                unrelated = Task(
                    workspace_id=parent.workspace_id,
                    title="Unrelated parent",
                    state=TaskState.COMPLETED.value,
                    assigned_agent_id=self.agent.id,
                    correlation_id=new_uuid7(),
                )
                session.add(unrelated)
                await session.flush()
                child_parent_id = unrelated.id
            child = Task(
                workspace_id=parent.workspace_id,
                title="Engineering implementation child",
                state=(
                    TaskState.FAILED.value
                    if status == "implementation_failed"
                    else TaskState.COMPLETED.value
                ),
                assigned_agent_id=self.agent.id,
                parent_task_id=child_parent_id,
                correlation_id=new_uuid7(),
                metadata_json={"origin": "engineering_template"},
            )
            session.add(child)
            await session.flush()
            run.task_id = child.id
            run.status = (
                RunStatus.FAILED.value
                if status == "implementation_failed"
                else RunStatus.COMPLETED.value
            )
            await session.commit()
            return child.id

    async def unrelated_task_id(self) -> UUID:
        async with self.sessions() as session:
            task = Task(
                workspace_id=self.workspace.id,
                title="Unrelated sync task",
                state=TaskState.COMPLETED.value,
                assigned_agent_id=self.agent.id,
                trigger_id=self.trigger.id,
                correlation_id=new_uuid7(),
            )
            session.add(task)
            await session.commit()
            return task.id


async def _build_sync_world(
    sessions: async_sessionmaker[AsyncSession],
) -> SyncWorld:
    publisher = _Publisher()
    resources = _Resources(session_factory=sessions, publisher=publisher)
    effect_bodies: list[str] = []
    fail_after_effect = [False]

    async def comment_effect(_context: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        parsed = CommentCreateInput.model_validate(payload.model_dump(mode="json"))
        effect_bodies.append(parsed.body)
        await asyncio.sleep(0)
        if fail_after_effect[0]:
            raise RuntimeError("provider response was lost after dispatch")
        return CommentCreateOutput(
            comment_id=f"comment-{len(effect_bodies)}",
            url="https://linear.test/comment/1",
        )

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="linear.comment.create",
            description="Create one Linear comment",
            risk=RiskLevel.WRITE,
            input_model=CommentCreateInput,
            output_model=CommentCreateOutput,
            required_capability="linear.comment.create",
        ),
        comment_effect,
    )

    async with sessions() as session:
        workspace = Workspace(name="Trigger sync", slug=f"trigger-sync-{new_uuid7().hex[:8]}")
        session.add(workspace)
        await session.flush()
        agent = Agent(workspace_id=workspace.id, name="Sync agent", slug="sync-agent")
        session.add(agent)
        secret = Secret(
            workspace_id=workspace.id,
            name="Linear credentials",
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
            connector_type="linear",
            name="Linear",
            auth_type="api_key",
            status=ConnectionStatus.ACTIVE.value,
            encrypted_secret_id=secret.id,
        )
        session.add(connection)
        await session.flush()
        trigger = Trigger(
            workspace_id=workspace.id,
            name="Linear sync",
            enabled=True,
            connection_id=connection.id,
            target_agent_id=agent.id,
            action_config_json={"comment_back": True},
        )
        session.add(trigger)
        await session.flush()
        task = Task(
            workspace_id=workspace.id,
            external_source="linear",
            external_id="ENG-77",
            title="Sync the result",
            state=TaskState.COMPLETED.value,
            assigned_agent_id=agent.id,
            trigger_id=trigger.id,
            correlation_id=new_uuid7(),
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace.id,
            agent_id=agent.id,
            task_id=task.id,
            status=RunStatus.COMPLETED.value,
        )
        session.add(run)
        await session.commit()

    return SyncWorld(
        activities=TriggerToolActivities(resources, catalog),  # type: ignore[arg-type]
        sessions=sessions,
        resources=resources,
        workspace=workspace,
        agent=agent,
        task=task,
        run=run,
        trigger=trigger,
        connection=connection,
        effect_bodies=effect_bodies,
        fail_after_effect=fail_after_effect,
    )


@pytest.fixture
async def sync_world() -> AsyncIterator[SyncWorld]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield await _build_sync_world(sessions)
    await engine.dispose()


@pytest.fixture
async def postgres_sync_world() -> AsyncIterator[SyncWorld]:
    host = os.environ.get("JHIN_POSTGRES_HOST", "127.0.0.1")
    port = int(os.environ.get("POSTGRES_DEV_PORT", "55432"))
    database_name = f"jhin_trigger_sync_{uuid4().hex}"
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
        yield await _build_sync_world(sessions)
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


async def test_sync_reloads_standing_authority_and_replays_one_claim(
    sync_world: SyncWorld,
) -> None:
    first = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
    second = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert (
        first == second == SyncExternalResult(synced=True, detail="https://linear.test/comment/1")
    )
    assert sync_world.effect_bodies == [sync_world.expected_body]
    row = await sync_world.tool_call()
    assert row is not None
    assert row.id == stable_sync_invocation_id(sync_world.run.id)
    assert row.tool_name == "system.trigger.sync_external"
    assert row.status == ToolCallStatus.COMPLETED.value
    assert row.sanitized_input_json == {
        "connection_id": str(sync_world.connection.id),
        "issue": "ENG-77",
        "body": sync_world.expected_body,
    }
    assert await sync_world.count_events("external.synced") == 1
    assert await sync_world.count_audits("trigger.synced_external") == 1
    async with sync_world.sessions() as session:
        grant_count = await session.scalar(select(func.count(AgentCapabilityGrant.id)))
    assert grant_count == 0


@pytest.mark.parametrize(
    "mutation",
    ["trigger_disabled", "trigger_renamed", "external_id_drift"],
)
async def test_terminal_sync_claim_replays_before_mutable_authority(
    sync_world: SyncWorld,
    mutation: str,
) -> None:
    first = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
    await sync_world.mutate_authority(mutation)

    replay = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert replay == first
    assert sync_world.effect_bodies == [sync_world.expected_body]
    assert await sync_world.count_events("external.synced") == 1


async def test_terminal_sync_claim_rejects_a_different_task_binding(
    sync_world: SyncWorld,
) -> None:
    await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
    unrelated_task_id = await sync_world.unrelated_task_id()

    with pytest.raises(ApplicationError) as error:
        await sync_world.activities.sync_external_tool_activity(
            SyncExternalToolInput(
                workspace_id=str(sync_world.workspace.id),
                task_id=str(unrelated_task_id),
                run_id=str(sync_world.run.id),
            )
        )

    assert error.value.type == "sync_invocation_mismatch"
    assert error.value.non_retryable is True
    assert sync_world.effect_bodies == [sync_world.expected_body]


async def test_concurrent_sync_calls_share_one_durable_effect(sync_world: SyncWorld) -> None:
    results = await asyncio.gather(
        sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params),
        sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params),
    )

    assert list(results) == [
        SyncExternalResult(synced=True, detail="https://linear.test/comment/1"),
        SyncExternalResult(synced=True, detail="https://linear.test/comment/1"),
    ]
    assert sync_world.effect_bodies == [sync_world.expected_body]


@pytest.mark.integration
async def test_postgres_concurrent_sync_calls_share_one_durable_effect(
    postgres_sync_world: SyncWorld,
) -> None:
    world = postgres_sync_world

    results = await asyncio.gather(
        *(world.activities.sync_external_tool_activity(world.ids_only_params) for _ in range(3))
    )

    assert results == [
        SyncExternalResult(synced=True, detail="https://linear.test/comment/1"),
        SyncExternalResult(synced=True, detail="https://linear.test/comment/1"),
        SyncExternalResult(synced=True, detail="https://linear.test/comment/1"),
    ]
    assert world.effect_bodies == [world.expected_body]
    assert await world.count_events("external.synced") == 1
    row = await world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.COMPLETED.value


@pytest.mark.parametrize(
    "case",
    ["trigger_disabled", "comment_back_disabled", "connection_disabled"],
)
async def test_sync_authority_failure_stops_before_claim_or_effect(
    sync_world: SyncWorld,
    case: str,
) -> None:
    await sync_world.mutate_authority(case)

    with pytest.raises(ApplicationError) as error:
        await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert error.value.type == "sync_authority_invalid"
    assert error.value.non_retryable is True
    assert sync_world.effect_bodies == []
    assert await sync_world.tool_call() is None


async def test_prior_executing_sync_claim_becomes_unknown_without_reposting(
    sync_world: SyncWorld,
) -> None:
    await sync_world.seed_executing_claim()

    for _attempt in range(2):
        with pytest.raises(ApplicationError) as error:
            await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
        assert error.value.type == "sync_execution_unknown"
        assert error.value.non_retryable is True

    row = await sync_world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert sync_world.effect_bodies == []
    assert await sync_world.count_events("external.sync_unknown") == 1
    assert await sync_world.count_audits("tool.call.execution_unknown") == 1


async def test_executing_sync_claim_becomes_unknown_after_authority_revocation(
    sync_world: SyncWorld,
) -> None:
    await sync_world.seed_executing_claim()
    await sync_world.mutate_authority("revoked_and_drifted")

    with pytest.raises(ApplicationError) as error:
        await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert error.value.type == "sync_execution_unknown"
    assert error.value.non_retryable is True
    row = await sync_world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert sync_world.effect_bodies == []
    assert await sync_world.count_events("external.sync_unknown") == 1
    assert (await sync_world.event_payload("external.sync_unknown"))["external_id"] == "ENG-77"


@pytest.mark.parametrize(
    "engineering_status",
    ["completed", "review_failed", "implementation_failed"],
)
async def test_engineering_child_sync_uses_parent_authority_and_final_status(
    sync_world: SyncWorld,
    engineering_status: str,
) -> None:
    await sync_world.configure_engineering_run(engineering_status)

    result = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert result == SyncExternalResult(
        synced=True,
        detail="https://linear.test/comment/1",
    )
    assert len(sync_world.effect_bodies) == 1
    assert sync_world.effect_bodies[0].endswith(f"({engineering_status}).")
    audit = await sync_world.latest_audit_metadata("trigger.synced_external")
    assert audit["run_status"] == engineering_status


async def test_engineering_child_sync_rejects_wrong_parent_before_effect(
    sync_world: SyncWorld,
) -> None:
    await sync_world.configure_engineering_run("completed", valid_parent=False)

    with pytest.raises(ApplicationError) as error:
        await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert error.value.type == "sync_authority_invalid"
    assert error.value.non_retryable is True
    assert sync_world.effect_bodies == []
    assert await sync_world.tool_call() is None


async def test_direct_engineering_sync_keeps_parent_run_authority(
    sync_world: SyncWorld,
) -> None:
    await sync_world.configure_engineering_run("completed", child_run=False)

    result = await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)

    assert result.synced is True
    assert sync_world.effect_bodies == [sync_world.expected_body]


async def test_lost_sync_response_is_unknown_and_never_retried(
    sync_world: SyncWorld,
) -> None:
    sync_world.fail_after_effect[0] = True

    for _attempt in range(2):
        with pytest.raises(ApplicationError) as error:
            await sync_world.activities.sync_external_tool_activity(sync_world.ids_only_params)
        assert error.value.type == "sync_execution_unknown"

    row = await sync_world.tool_call()
    assert row is not None
    assert row.status == ToolCallStatus.EXECUTION_UNKNOWN.value
    assert sync_world.effect_bodies == [sync_world.expected_body]
    assert await sync_world.count_events("external.sync_unknown") == 1


@pytest.mark.parametrize("delete_result", [True, False])
async def test_cleanup_uses_run_workspace_name_once(
    sync_world: SyncWorld,
    delete_result: bool,
) -> None:
    deleted_names: list[str] = []

    async def delete_workspace(workspace_name: str) -> bool:
        deleted_names.append(workspace_name)
        return delete_result

    activities = CleanupActivities(
        sync_world.resources,  # type: ignore[arg-type]
        delete_workspace=delete_workspace,
    )
    params = CleanupRunWorkspaceInput(
        workspace_id=str(sync_world.workspace.id),
        run_id=str(sync_world.run.id),
    )

    first = await activities.cleanup_run_workspace_activity(params)
    second = await activities.cleanup_run_workspace_activity(params)

    assert first == CleanupRunWorkspaceResult(deleted=delete_result)
    assert second == CleanupRunWorkspaceResult(deleted=False)
    assert deleted_names == [f"run-{sync_world.run.id}"]


async def test_cleanup_rejects_invalid_identity_before_runner_call(
    sync_world: SyncWorld,
) -> None:
    calls: list[str] = []

    async def delete_workspace(workspace_name: str) -> bool:
        calls.append(workspace_name)
        return True

    activities = CleanupActivities(
        sync_world.resources,  # type: ignore[arg-type]
        delete_workspace=delete_workspace,
    )

    with pytest.raises(ApplicationError) as error:
        await activities.cleanup_run_workspace_activity(
            CleanupRunWorkspaceInput(
                workspace_id="not-a-uuid",
                run_id=str(sync_world.run.id),
            )
        )

    assert error.value.type == "cleanup_identity_invalid"
    assert error.value.non_retryable is True
    assert calls == []


async def test_cleanup_rejects_cross_workspace_run_before_runner_call(
    sync_world: SyncWorld,
) -> None:
    calls: list[str] = []

    async def delete_workspace(workspace_name: str) -> bool:
        calls.append(workspace_name)
        return True

    activities = CleanupActivities(
        sync_world.resources,  # type: ignore[arg-type]
        delete_workspace=delete_workspace,
    )

    with pytest.raises(ApplicationError) as error:
        await activities.cleanup_run_workspace_activity(
            CleanupRunWorkspaceInput(
                workspace_id=str(new_uuid7()),
                run_id=str(sync_world.run.id),
            )
        )

    assert error.value.type == "cleanup_context_invalid"
    assert error.value.non_retryable is True
    assert calls == []


async def test_direct_cleanup_workflow_cannot_bypass_database_binding(
    sync_world: SyncWorld,
) -> None:
    calls: list[str] = []

    async def delete_workspace(workspace_name: str) -> bool:
        calls.append(workspace_name)
        return True

    activities = CleanupActivities(
        sync_world.resources,  # type: ignore[arg-type]
        delete_workspace=delete_workspace,
    )
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(
            environment.client,
            task_queue=TOOL_TASK_QUEUE,
            workflows=[CleanupCompatibilityWorkflow],
            activities=[activities.cleanup_run_workspace_activity],
        ):
            with pytest.raises(WorkflowFailureError):
                await environment.client.execute_workflow(
                    CleanupCompatibilityWorkflow.run,
                    CleanupRunWorkspaceInput(
                        workspace_id=str(new_uuid7()),
                        run_id=str(sync_world.run.id),
                    ),
                    id=f"direct-cleanup-{new_uuid7()}",
                    task_queue=TOOL_TASK_QUEUE,
                )
    finally:
        await environment.shutdown()

    assert calls == []
