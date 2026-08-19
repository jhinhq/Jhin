"""Authoritative, at-most-once trigger comment-back on the tool worker."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_db.models import (
    Agent,
    AgentRun,
    AuditEvent,
    Connection,
    RunEvent,
    Secret,
    Task,
    ToolCall,
    Trigger,
)
from jhin_domain import (
    RUN_TERMINAL_STATUSES,
    TASK_TERMINAL_STATES,
    ActorType,
    ConnectionStatus,
    SecretType,
    ToolCallStatus,
)
from jhin_events import EventEnvelope, EventSource
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tools import (
    ToolCatalog,
    ToolExecutionContext,
    sanitize_payload,
    stable_sync_invocation_id,
)
from jhin_workflows.tool_compat import SyncExternalToolInput
from jhin_workflows.triggered_task.shared import (
    ACTIVITY_SYNC_EXTERNAL_TOOL,
    SyncExternalResult,
)

logger = logging.getLogger(__name__)

_SYNC_TOOL_NAME = "system.trigger.sync_external"
_CONNECTOR_TOOL_NAME = "linear.comment.create"
_TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolCallStatus.COMPLETED.value,
        ToolCallStatus.FAILED.value,
        ToolCallStatus.DENIED.value,
        ToolCallStatus.REJECTED.value,
    }
)
_STATUS_LINES = {
    "completed": "completed the task",
    "failed": "could not complete the task",
    "cancelled": "was cancelled before finishing",
}
_PROCESS_SYNC_LOCKS: dict[UUID, asyncio.Lock] = {}
_PROCESS_SYNC_LOCKS_GUARD = asyncio.Lock()


@dataclass(frozen=True)
class _SyncAuthority:
    workspace_id: UUID
    task_id: UUID
    run_id: UUID
    agent_id: UUID
    agent_name: str
    trigger_name: str
    connection_id: UUID
    external_source: str
    external_id: str
    run_status: str
    input_payload: dict[str, object]


def _identity_error(message: str) -> ApplicationError:
    return ApplicationError(
        message,
        type="sync_identity_invalid",
        non_retryable=True,
    )


def _uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise _identity_error(f"sync {field} is not a UUID") from error


def _authority_error(message: str) -> ApplicationError:
    return ApplicationError(
        message,
        type="sync_authority_invalid",
        non_retryable=True,
    )


def _unknown_error(invocation_id: UUID) -> ApplicationError:
    return ApplicationError(
        f"trigger sync {invocation_id} may have produced an external effect; "
        "manual reconciliation is required",
        type="sync_execution_unknown",
        non_retryable=True,
    )


class TriggerToolActivities:
    def __init__(self, resources: ToolWorkerResources, catalog: ToolCatalog) -> None:
        self._resources = resources
        self._catalog = catalog

    async def _publish(
        self,
        authority: _SyncAuthority,
        *,
        detail: str,
    ) -> None:
        try:
            await self._resources.publisher.publish(
                EventEnvelope(
                    event_type="trigger.synced_external",
                    workspace_id=str(authority.workspace_id),
                    source=EventSource(
                        type="tool_worker",
                        connection_id=authority.connection_id,
                    ),
                    data={
                        "task_id": str(authority.task_id),
                        "run_id": str(authority.run_id),
                        "external_source": authority.external_source,
                        "external_id": authority.external_id,
                        "run_status": authority.run_status,
                        "detail": detail,
                    },
                )
            )
        except Exception as error:
            logger.warning(
                "Trigger sync publish failed (%s)",
                type(error).__name__[:100],
            )

    @asynccontextmanager
    async def _lifecycle_session(self, invocation_id: UUID) -> AsyncIterator[AsyncSession]:
        async with self._resources.session_factory() as initial_session:
            bind = initial_session.bind
        if isinstance(bind, AsyncEngine) and bind.dialect.name == "postgresql":
            advisory_key = int.from_bytes(invocation_id.bytes[:8], "big", signed=True)
            async with bind.connect() as lock_connection:
                await lock_connection.scalar(select(func.pg_advisory_lock(advisory_key)))
                await lock_connection.commit()
                try:
                    async with AsyncSession(
                        bind=lock_connection,
                        expire_on_commit=False,
                    ) as session:
                        yield session
                finally:
                    await lock_connection.scalar(select(func.pg_advisory_unlock(advisory_key)))
                    await lock_connection.commit()
            return

        async with _PROCESS_SYNC_LOCKS_GUARD:
            lock = _PROCESS_SYNC_LOCKS.setdefault(invocation_id, asyncio.Lock())
        async with lock, self._resources.session_factory() as session:
            yield session

    async def _load_authority(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        task_id: UUID,
        run_id: UUID,
    ) -> _SyncAuthority:
        row = (
            await session.execute(
                select(Task, AgentRun, Trigger, Connection, Agent, Secret)
                .join(
                    AgentRun,
                    (AgentRun.task_id == Task.id) & (AgentRun.workspace_id == Task.workspace_id),
                )
                .join(
                    Trigger,
                    (Trigger.id == Task.trigger_id) & (Trigger.workspace_id == Task.workspace_id),
                )
                .join(
                    Connection,
                    (Connection.id == Trigger.connection_id)
                    & (Connection.workspace_id == Task.workspace_id),
                )
                .join(
                    Agent,
                    (Agent.id == AgentRun.agent_id) & (Agent.workspace_id == Task.workspace_id),
                )
                .join(
                    Secret,
                    (Secret.id == Connection.encrypted_secret_id)
                    & (Secret.workspace_id == Task.workspace_id),
                )
                .where(
                    Task.id == task_id,
                    Task.workspace_id == workspace_id,
                    AgentRun.id == run_id,
                )
                .limit(2)
            )
        ).all()
        if len(row) != 1:
            raise _authority_error("trigger sync authority was not found")
        task, run, trigger, connection, agent, secret = row[0]
        if (
            trigger.enabled is not True
            or trigger.action_config_json.get("comment_back") is not True
            or connection.status != ConnectionStatus.ACTIVE.value
            or connection.connector_type != "linear"
            or secret.type != SecretType.CONNECTION_CREDENTIALS.value
            or task.external_source != "linear"
            or not isinstance(task.external_id, str)
            or not task.external_id
            or run.status not in {status.value for status in RUN_TERMINAL_STATUSES}
            or task.state not in {state.value for state in TASK_TERMINAL_STATES}
        ):
            raise _authority_error("trigger sync standing authority is no longer valid")

        status_line = _STATUS_LINES.get(run.status, run.status)
        body = (
            f"**Jhin** — trigger “{trigger.name}”: the assigned agent {status_line}. "
            f"Task `{task.id}` ({run.status})."
        )
        raw_input: dict[str, object] = {
            "connection_id": str(connection.id),
            "issue": task.external_id,
            "body": body,
        }
        registered = self._catalog.get(_CONNECTOR_TOOL_NAME)
        if registered is None:
            raise _authority_error("Linear comment sync is not registered")
        definition, _executor = registered
        try:
            validated = definition.input_model.model_validate(raw_input)
        except ValidationError as error:
            raise _authority_error("trigger sync input no longer matches its connector") from error
        dumped = validated.model_dump(mode="json")
        sanitized = sanitize_payload(dumped)
        if sanitized != dumped:
            raise _authority_error("trigger sync input could not be stored losslessly")
        return _SyncAuthority(
            workspace_id=workspace_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent.id,
            agent_name=agent.name,
            trigger_name=trigger.name,
            connection_id=connection.id,
            external_source=task.external_source,
            external_id=task.external_id,
            run_status=run.status,
            input_payload=sanitized,
        )

    @staticmethod
    def _claim_matches(
        row: ToolCall,
        authority: _SyncAuthority,
        invocation_id: UUID,
    ) -> bool:
        return (
            row.id == invocation_id
            and row.workspace_id == authority.workspace_id
            and row.run_id == authority.run_id
            and row.agent_id == authority.agent_id
            and row.tool_name == _SYNC_TOOL_NAME
            and row.connection_id == authority.connection_id
            and row.sanitized_input_json == authority.input_payload
        )

    async def _append_event(
        self,
        session: AsyncSession,
        authority: _SyncAuthority,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        run = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.id == authority.run_id,
                AgentRun.workspace_id == authority.workspace_id,
                AgentRun.task_id == authority.task_id,
                AgentRun.agent_id == authority.agent_id,
            )
            .with_for_update()
        )
        if run is None:
            raise _authority_error("trigger sync run authority disappeared")
        current = await session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == authority.run_id)
        )
        session.add(
            RunEvent(
                workspace_id=authority.workspace_id,
                run_id=authority.run_id,
                task_id=authority.task_id,
                seq=(current if current is not None else -1) + 1,
                event_type=event_type,
                payload_json=payload,
            )
        )

    async def _mark_unknown(
        self,
        session: AsyncSession,
        authority: _SyncAuthority,
        invocation_id: UUID,
    ) -> SyncExternalResult | None:
        await session.rollback()
        row = await session.scalar(
            select(ToolCall).where(ToolCall.id == invocation_id).with_for_update()
        )
        if row is None or not self._claim_matches(row, authority, invocation_id):
            raise ApplicationError(
                "trigger sync invocation binding changed",
                type="sync_invocation_mismatch",
                non_retryable=True,
            )
        if row.status == ToolCallStatus.COMPLETED.value:
            detail = row.sanitized_output_json.get("url", "")
            return SyncExternalResult(
                synced=True,
                detail=detail if isinstance(detail, str) else "",
            )
        if row.status == ToolCallStatus.EXECUTION_UNKNOWN.value:
            return None
        if row.status != ToolCallStatus.EXECUTING.value:
            raise ApplicationError(
                "trigger sync claim has an unexpected status",
                type="sync_invocation_mismatch",
                non_retryable=True,
            )
        row.status = ToolCallStatus.EXECUTION_UNKNOWN.value
        row.completed_at = datetime.now(UTC)
        row.error_code = "execution_outcome_unknown"
        await self._append_event(
            session,
            authority,
            event_type="external.sync_unknown",
            payload={
                "external_source": authority.external_source,
                "external_id": authority.external_id,
                "tool_call_id": str(invocation_id),
                "manual_reconciliation_required": True,
            },
        )
        session.add(
            AuditEvent(
                workspace_id=authority.workspace_id,
                actor_type=ActorType.SYSTEM.value,
                actor_id=None,
                action="tool.call.execution_unknown",
                target_type="tool_call",
                target_id=invocation_id,
                metadata_json={
                    "run_id": str(authority.run_id),
                    "task_id": str(authority.task_id),
                    "tool_name": _SYNC_TOOL_NAME,
                    "code": "execution_outcome_unknown",
                },
            )
        )
        await session.commit()
        return None

    async def _existing_result(
        self,
        session: AsyncSession,
        authority: _SyncAuthority,
        invocation_id: UUID,
    ) -> SyncExternalResult | None:
        row = await session.scalar(
            select(ToolCall).where(ToolCall.id == invocation_id).with_for_update()
        )
        if row is None:
            return None
        if not self._claim_matches(row, authority, invocation_id):
            raise ApplicationError(
                "trigger sync invocation binding changed",
                type="sync_invocation_mismatch",
                non_retryable=True,
            )
        if row.status == ToolCallStatus.COMPLETED.value:
            detail = row.sanitized_output_json.get("url", "")
            await session.rollback()
            return SyncExternalResult(
                synced=True,
                detail=detail if isinstance(detail, str) else "",
            )
        if row.status == ToolCallStatus.FAILED.value:
            error_code = row.error_code or "sync_failed"
            await session.rollback()
            return SyncExternalResult(synced=False, detail=error_code)
        if row.status in {
            ToolCallStatus.EXECUTING.value,
            ToolCallStatus.EXECUTION_UNKNOWN.value,
        }:
            await self._mark_unknown(session, authority, invocation_id)
            raise _unknown_error(invocation_id)
        if row.status in _TERMINAL_TOOL_STATUSES:
            raise ApplicationError(
                "trigger sync claim has an invalid terminal status",
                type="sync_invocation_mismatch",
                non_retryable=True,
            )
        raise ApplicationError(
            "trigger sync claim has an unexpected status",
            type="sync_invocation_mismatch",
            non_retryable=True,
        )

    async def _execute_claim(
        self,
        session: AsyncSession,
        authority: _SyncAuthority,
        invocation_id: UUID,
    ) -> SyncExternalResult:
        registered = self._catalog.get(_CONNECTOR_TOOL_NAME)
        if registered is None:
            raise _authority_error("Linear comment sync is not registered")
        definition, executor = registered
        validated_input = definition.input_model.model_validate(authority.input_payload)

        claim = ToolCall(
            id=invocation_id,
            workspace_id=authority.workspace_id,
            run_id=authority.run_id,
            agent_id=authority.agent_id,
            tool_name=_SYNC_TOOL_NAME,
            connection_id=authority.connection_id,
            sanitized_input_json=authority.input_payload,
            sanitized_output_json={},
            status=ToolCallStatus.EXECUTING.value,
            started_at=datetime.now(UTC),
        )
        session.add(claim)
        session.add(
            AuditEvent(
                workspace_id=authority.workspace_id,
                actor_type=ActorType.SYSTEM.value,
                actor_id=None,
                action="tool.call.requested",
                target_type="tool_call",
                target_id=invocation_id,
                metadata_json={
                    "run_id": str(authority.run_id),
                    "task_id": str(authority.task_id),
                    "tool_name": _SYNC_TOOL_NAME,
                },
            )
        )
        await session.commit()

        started = time.monotonic()
        try:
            output = await executor(
                ToolExecutionContext(
                    session=session,
                    workspace_id=authority.workspace_id,
                    task_id=authority.task_id,
                    run_id=authority.run_id,
                    agent_id=authority.agent_id,
                    agent_name=authority.agent_name,
                    crypto=self._resources.crypto,
                    session_factory=self._resources.session_factory,
                    tool_call_id=invocation_id,
                    test_barrier=self._resources.test_barrier,
                ),
                validated_input,
            )
            if not isinstance(output, BaseModel):
                raise ValueError("sync executor returned a non-model output")
            validated_output = definition.output_model.model_validate(
                output.model_dump(mode="json")
            )
            dumped_output = validated_output.model_dump(mode="json")
            sanitized_output = sanitize_payload(dumped_output)
            if sanitized_output != dumped_output:
                raise ValueError("sync output could not be stored losslessly")

            row = await session.scalar(
                select(ToolCall).where(ToolCall.id == invocation_id).with_for_update()
            )
            if row is None or not self._claim_matches(row, authority, invocation_id):
                raise RuntimeError("trigger sync claim disappeared after dispatch")
            row.status = ToolCallStatus.COMPLETED.value
            row.sanitized_output_json = sanitized_output
            row.completed_at = datetime.now(UTC)
            row.duration_ms = max(0, int((time.monotonic() - started) * 1000))
            row.error_code = None
            detail_value = sanitized_output.get("url", "")
            detail = detail_value if isinstance(detail_value, str) else ""
            await self._append_event(
                session,
                authority,
                event_type="external.synced",
                payload={
                    "external_source": authority.external_source,
                    "external_id": authority.external_id,
                    "detail": detail,
                    "tool_call_id": str(invocation_id),
                },
            )
            session.add(
                AuditEvent(
                    workspace_id=authority.workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action="trigger.synced_external",
                    target_type="task",
                    target_id=authority.task_id,
                    metadata_json={
                        "external_source": authority.external_source,
                        "external_id": authority.external_id,
                        "run_status": authority.run_status,
                        "comment_url": detail,
                        "tool_call_id": str(invocation_id),
                    },
                )
            )
            await session.commit()
        except BaseException as error:
            recovered = await self._mark_unknown(session, authority, invocation_id)
            if recovered is not None:
                return recovered
            if isinstance(error, Exception):
                raise _unknown_error(invocation_id) from error
            raise

        await self._publish(authority, detail=detail)
        return SyncExternalResult(synced=True, detail=detail)

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL_TOOL)
    async def sync_external_tool_activity(
        self,
        params: SyncExternalToolInput,
    ) -> SyncExternalResult:
        workspace_id = _uuid(params.workspace_id, field="workspace_id")
        task_id = _uuid(params.task_id, field="task_id")
        run_id = _uuid(params.run_id, field="run_id")
        invocation_id = stable_sync_invocation_id(run_id)
        async with self._lifecycle_session(invocation_id) as session:
            authority = await self._load_authority(
                session,
                workspace_id=workspace_id,
                task_id=task_id,
                run_id=run_id,
            )
            replayed = await self._existing_result(session, authority, invocation_id)
            if replayed is not None:
                return replayed
            return await self._execute_claim(session, authority, invocation_id)


__all__ = ["TriggerToolActivities"]
