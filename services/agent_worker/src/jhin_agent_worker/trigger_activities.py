"""Activities behind TriggeredTaskWorkflow (plan 8.1, 26).

``prepare_triggered_task`` creates (or dedupe-loads) the externally-linked
task row and links it to the trigger invocation. ``sync_external`` posts the
run outcome back to the source system through the connector's own tool
executor.

Authorization model for sync-back (plan 26.14): the sync runs as a *system
actor*, not as the agent — it does not consume the agent's capability grants
and never enters the approval gateway. Its authority derives from the
trigger definition itself: a workspace member with trigger-management rights
enabled ``comment_back`` on an audited trigger, which constitutes standing
approval for exactly this action (one comment, on the entity that fired the
trigger, over the trigger's own connection). The action is recorded in the
run timeline and the audit log.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_connectors.linear.schemas import CommentCreateInput, CommentCreateOutput
from jhin_connectors.linear.tools import LINEAR_TOOLS
from jhin_db.models import AuditEvent, Message, RunEvent, Task, Trigger, TriggerInvocation
from jhin_domain import (
    MessageVisibility,
    RecipientType,
    SenderType,
    TaskState,
    new_uuid7,
)
from jhin_events import EventEnvelope, EventSource
from jhin_observability import get_logger
from jhin_secrets.redaction import redact_text
from jhin_tools import PHASE9_SYNC_BEFORE_EFFECT, ToolExecutionContext
from jhin_workflows.triggered_task import (
    ACTIVITY_PREPARE_TRIGGERED_TASK,
    ACTIVITY_SYNC_EXTERNAL,
    PreparedTask,
    SyncExternalInput,
    SyncExternalResult,
    TriggeredTaskInput,
)

logger = get_logger(__name__)

_ACTIVE_TASK_STATES = (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)

_STATUS_LINES = {
    "completed": "completed the task",
    "failed": "could not complete the task",
    "cancelled": "was cancelled before finishing",
}


class TriggerActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    async def _publish(self, workspace_id: UUID, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self._resources.publisher.publish(
                EventEnvelope(
                    event_type=event_type,
                    workspace_id=str(workspace_id),
                    source=EventSource(type="agent_worker"),
                    data=data,
                )
            )
        except Exception as exc:
            logger.warning(
                "events.publish_failed", event_type=event_type, error=f"{type(exc).__name__}"
            )

    @activity.defn(name=ACTIVITY_PREPARE_TRIGGERED_TASK)
    async def prepare_triggered_task_activity(self, params: TriggeredTaskInput) -> PreparedTask:
        workspace_id = UUID(params.workspace_id)
        trigger_id = UUID(params.trigger_id)
        agent_id = UUID(params.agent_id)

        async with self._resources.session_factory() as session:
            trigger = await session.scalar(
                select(Trigger).where(
                    Trigger.id == trigger_id, Trigger.workspace_id == workspace_id
                )
            )
            if trigger is None:
                raise ApplicationError(
                    "trigger no longer exists", type="trigger_not_found", non_retryable=True
                )

            # Task-level dedupe (plan 26.8): one *active* task per external
            # entity. A finished task does not block a re-trigger later.
            existing = await session.scalar(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.external_source == params.external_source,
                    Task.external_id == params.external_id,
                    Task.state.in_(_ACTIVE_TASK_STATES),
                )
            )
            if existing is not None:
                await self._link_invocation(session, params, existing.id)
                await session.commit()
                logger.info(
                    "trigger.task_deduped",
                    task_id=str(existing.id),
                    external_id=params.external_id,
                )
                return PreparedTask(task_id=str(existing.id), created=False)

            title = params.title or f"{params.external_source}:{params.external_id}"
            task = Task(
                workspace_id=workspace_id,
                external_source=params.external_source,
                external_id=params.external_id,
                title=title[:500],
                description=params.description,
                state=TaskState.QUEUED.value,
                assigned_agent_id=agent_id,
                trigger_id=trigger_id,
                correlation_id=new_uuid7(),
                metadata_json={
                    "origin": "trigger",
                    "trigger_id": params.trigger_id,
                    "trigger_name": params.trigger_name,
                    "external_source": params.external_source,
                    "external_id": params.external_id,
                    "external_url": params.external_url,
                    "event_id": params.event_id,
                },
            )
            session.add(task)
            await session.flush()
            # The child AgentTaskWorkflow runs under the API's id convention
            # so existing signal endpoints work on triggered tasks.
            task.temporal_workflow_id = f"task-{task.id}"

            origin = (
                f"Started by trigger \u201c{params.trigger_name}\u201d from "
                f"{params.external_source} {params.external_id}"
            )
            if params.external_url:
                origin += f" ({params.external_url})"
            session.add(
                Message(
                    workspace_id=workspace_id,
                    task_id=task.id,
                    sender_type=SenderType.SYSTEM.value,
                    sender_id=None,
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=task.id,
                    message_type="text",
                    content_json={"text": origin},
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )
            await self._link_invocation(session, params, task.id)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type="system",
                    actor_id=None,
                    action="task.created",
                    target_type="task",
                    target_id=task.id,
                    metadata_json={
                        "origin": "trigger",
                        "trigger_id": params.trigger_id,
                        "trigger_name": params.trigger_name,
                        "external_id": params.external_id,
                    },
                )
            )
            await session.commit()
            task_id = task.id

        await self._publish(
            workspace_id,
            "task.created",
            {
                "task_id": str(task_id),
                "trigger_id": params.trigger_id,
                "external_source": params.external_source,
                "external_id": params.external_id,
                "agent_id": params.agent_id,
            },
        )
        return PreparedTask(task_id=str(task_id), created=True)

    async def _link_invocation(
        self, session: AsyncSession, params: TriggeredTaskInput, task_id: UUID
    ) -> None:
        invocation = await session.scalar(
            select(TriggerInvocation).where(
                TriggerInvocation.id == UUID(params.invocation_id),
                TriggerInvocation.workspace_id == UUID(params.workspace_id),
            )
        )
        if invocation is not None:
            invocation.task_id = task_id

    @activity.defn(name=ACTIVITY_SYNC_EXTERNAL)
    async def sync_external_activity(self, params: SyncExternalInput) -> SyncExternalResult:
        """Post the outcome back to the source entity (comment-back).

        Connector dispatch happens here — never in the generic trigger
        engine (plan 52). Currently only Linear sync-back is supported;
        other sources report unsupported instead of failing the workflow.
        """
        if params.external_source != "linear":
            return SyncExternalResult(
                synced=False, detail=f"no sync-back support for {params.external_source!r}"
            )

        workspace_id = UUID(params.workspace_id)
        status_line = _STATUS_LINES.get(params.run_status, params.run_status)
        body = (
            f"**Jhin** \u2014 trigger \u201c{params.trigger_name}\u201d: the assigned agent "
            f"{status_line}. Task `{params.task_id}` ({params.run_status})."
        )

        executor = next(
            executor
            for definition, executor in LINEAR_TOOLS
            if definition.name == "linear.comment.create"
        )
        async with self._resources.session_factory() as session:
            ctx = ToolExecutionContext(
                session=session,
                workspace_id=workspace_id,
                task_id=UUID(params.task_id),
                run_id=UUID(params.run_id),
                agent_id=UUID(params.agent_id),
                agent_name="system",
                crypto=self._resources.crypto,
                test_barrier=getattr(self._resources, "test_barrier", None),
            )
            try:
                if ctx.test_barrier is not None:
                    await ctx.test_barrier.arrive_and_wait(
                        PHASE9_SYNC_BEFORE_EFFECT, UUID(params.run_id)
                    )
                output = await executor(
                    ctx,
                    CommentCreateInput(
                        connection_id=params.connection_id,
                        issue=params.external_id,
                        body=body,
                    ),
                )
            except Exception as exc:
                detail = redact_text(f"{type(exc).__name__}: {exc}")[:500]
                await self._record_sync_event(session, params, ok=False, detail=detail)
                await session.commit()
                # Raise so the retry policy applies; the workflow treats
                # exhaustion as synced=False.
                raise ApplicationError(detail, type="sync_external_failed") from exc

            comment = output if isinstance(output, CommentCreateOutput) else None
            url = comment.url if comment is not None else ""
            await self._record_sync_event(session, params, ok=True, detail=url)
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type="system",
                    actor_id=None,
                    action="trigger.synced_external",
                    target_type="task",
                    target_id=UUID(params.task_id),
                    metadata_json={
                        "external_source": params.external_source,
                        "external_id": params.external_id,
                        "run_status": params.run_status,
                        "comment_url": url,
                    },
                )
            )
            await session.commit()

        await self._publish(
            workspace_id,
            "trigger.synced_external",
            {
                "task_id": params.task_id,
                "external_source": params.external_source,
                "external_id": params.external_id,
                "run_status": params.run_status,
            },
        )
        return SyncExternalResult(synced=True, detail=url)

    async def _record_sync_event(
        self, session: AsyncSession, params: SyncExternalInput, *, ok: bool, detail: str
    ) -> None:
        run_id = UUID(params.run_id)
        current = await session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
        session.add(
            RunEvent(
                workspace_id=UUID(params.workspace_id),
                run_id=run_id,
                task_id=UUID(params.task_id),
                seq=(current if current is not None else -1) + 1,
                event_type="external.synced" if ok else "external.sync_failed",
                payload_json={
                    "external_source": params.external_source,
                    "external_id": params.external_id,
                    "detail": detail,
                },
            )
        )
