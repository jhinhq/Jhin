"""Agent run activities (plan 8.2): snapshot resolution, one reasoning step,
and final persistence.

Postgres is written first and is the source of truth; NATS publishes are
best-effort transport for live UI (plan 2.3, 9.1). Model credentials are
decrypted here — inside the activity, at the moment of use — and exist only
in process memory (plan 13.5). Error strings pass through the secret
redactor before persisting or raising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_agents import AgentExecutionSnapshot, resolve_snapshot
from jhin_agents.context import ConversationTurn, TaskContext
from jhin_agents.runtime import estimate_cost_micros, execute_step
from jhin_agents.snapshot import SnapshotError
from jhin_db.models import AgentRun, Message, RunEvent, Task
from jhin_domain import (
    MessageVisibility,
    RecipientType,
    RunStatus,
    SenderType,
    TaskState,
)
from jhin_events import EventEnvelope, EventSource
from jhin_models import ModelProviderError, build_model_client
from jhin_models.factory import ProviderConfigError
from jhin_observability import get_logger
from jhin_secrets import SecretStore
from jhin_secrets.redaction import redact_text
from jhin_workflows.agent_task import (
    ACTIVITY_FINALIZE_RUN,
    ACTIVITY_RESOLVE_SNAPSHOT,
    ACTIVITY_RUN_AGENT_STEP,
    AgentTaskInput,
    FinalizeInput,
    RunStepInput,
    SnapshotResult,
    StepResult,
)

logger = get_logger(__name__)


class AgentActivities:
    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    # --- Helpers ---

    async def _publish(self, workspace_id: UUID, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort NATS publish; the database already holds the fact."""
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

    async def _next_seq(self, session: AsyncSession, run_id: UUID) -> int:
        current = await session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
        return (current if current is not None else -1) + 1

    def _add_run_event(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        task_id: UUID | None,
        seq: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            RunEvent(
                workspace_id=workspace_id,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
                event_type=event_type,
                payload_json=payload,
            )
        )

    # --- Activities ---

    @activity.defn(name=ACTIVITY_RESOLVE_SNAPSHOT)
    async def resolve_snapshot_activity(self, params: AgentTaskInput) -> SnapshotResult:
        workspace_id = UUID(params.workspace_id)
        agent_id = UUID(params.agent_id)
        task_id = UUID(params.task_id)
        info = activity.info()

        async with self._resources.session_factory() as session:
            try:
                snapshot = await resolve_snapshot(session, workspace_id, agent_id)
            except SnapshotError as exc:
                raise ApplicationError(str(exc), type=exc.code, non_retryable=True) from exc

            run = AgentRun(
                workspace_id=workspace_id,
                agent_id=agent_id,
                task_id=task_id,
                status=RunStatus.RUNNING.value,
                model_profile_id=snapshot.model_profile.profile_id,
                snapshot_hash=snapshot.snapshot_hash(),
                started_at=datetime.now(UTC),
                temporal_workflow_id=info.workflow_id,
                temporal_run_id=info.workflow_run_id,
            )
            session.add(run)
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is not None:
                task.state = TaskState.RUNNING.value
            await session.flush()
            self._add_run_event(
                session,
                workspace_id=workspace_id,
                run_id=run.id,
                task_id=task_id,
                seq=0,
                event_type="run.started",
                payload={
                    "agent_name": snapshot.name,
                    "model_profile": snapshot.model_profile.display_name,
                    "model_name": snapshot.model_profile.model_name,
                    "snapshot_hash": snapshot.snapshot_hash(),
                },
            )
            await session.commit()
            run_id = run.id

        await self._publish(
            workspace_id,
            "agent.run.started",
            {"run_id": str(run_id), "task_id": params.task_id, "agent_id": params.agent_id},
        )
        return SnapshotResult(
            run_id=str(run_id),
            snapshot_json=snapshot.model_dump_json(),
            snapshot_hash=snapshot.snapshot_hash(),
            max_steps=snapshot.run_limits.max_steps,
        )

    @activity.defn(name=ACTIVITY_RUN_AGENT_STEP)
    async def run_agent_step_activity(self, params: RunStepInput) -> StepResult:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        run_id = UUID(params.run_id)
        snapshot = AgentExecutionSnapshot.model_validate_json(params.snapshot_json)

        async with self._resources.session_factory() as session:
            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is None:
                raise ApplicationError("task not found", type="task_not_found", non_retryable=True)

            history = await self._load_history(session, task)

            # Decrypt the provider credential at the moment of use (13.5).
            api_key: str | None = None
            if snapshot.model_profile.secret_id is not None:
                api_key = await SecretStore(session, self._resources.crypto).reveal(
                    workspace_id, snapshot.model_profile.secret_id
                )

            try:
                client = build_model_client(
                    snapshot.model_profile.provider_type,
                    base_url=snapshot.model_profile.base_url,
                    api_key=api_key,
                )
            except ProviderConfigError as exc:
                raise ApplicationError(
                    redact_text(str(exc)), type="provider_config", non_retryable=True
                ) from exc
            del api_key  # plaintext lives only until the client holds it

            try:
                outcome = await execute_step(
                    client,
                    snapshot,
                    TaskContext(
                        title=task.title,
                        description=task.description,
                        history=history,
                        user_instructions=tuple(params.user_instructions),
                    ),
                )
            except ModelProviderError as exc:
                raise ApplicationError(
                    redact_text(str(exc)),
                    type="model_provider_error",
                    non_retryable=not exc.retryable,
                ) from exc
            finally:
                await client.close()

            cost_micros = estimate_cost_micros(
                outcome.usage,
                snapshot.model_profile.input_cost_micros_per_million,
                snapshot.model_profile.output_cost_micros_per_million,
            )

            # Persist everything in one transaction; commit is the final
            # failable step so activity retries cannot double-write.
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.input_tokens += outcome.usage.input_tokens
                run.output_tokens += outcome.usage.output_tokens
                run.cached_tokens += outcome.usage.cached_tokens
                run.estimated_cost_micros += cost_micros
                run.steps_used = params.step_index + 1

            session.add(
                Message(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    run_id=run_id,
                    sender_type=SenderType.AGENT.value,
                    sender_id=UUID(params.agent_id),
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=task_id,
                    message_type="text",
                    content_json={"text": outcome.text, "finish_reason": outcome.finish_reason},
                    visibility=MessageVisibility.VISIBLE.value,
                )
            )

            seq = await self._next_seq(session, run_id)
            for transition in outcome.transitions:
                payload: dict[str, Any] = {"detail": transition.detail, "step": params.step_index}
                if transition.node == "reason":
                    payload.update(
                        {
                            "model": outcome.model,
                            "input_tokens": outcome.usage.input_tokens,
                            "output_tokens": outcome.usage.output_tokens,
                            "cached_tokens": outcome.usage.cached_tokens,
                            "cost_micros": cost_micros,
                            "latency_ms": outcome.latency_ms,
                            "provider_request_id": outcome.provider_request_id,
                        }
                    )
                self._add_run_event(
                    session,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    task_id=task_id,
                    seq=seq,
                    event_type=f"node.{transition.node}",
                    payload=payload,
                )
                seq += 1
            await session.commit()

        await self._publish(
            workspace_id,
            "agent.run.step",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "step": params.step_index,
                "done": outcome.done,
            },
        )
        return StepResult(
            done=outcome.done,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cached_tokens=outcome.usage.cached_tokens,
            cost_micros=cost_micros,
        )

    async def _load_history(
        self, session: AsyncSession, task: Task
    ) -> tuple[ConversationTurn, ...]:
        rows = await session.scalars(
            select(Message)
            .where(
                Message.task_id == task.id,
                Message.visibility == MessageVisibility.VISIBLE.value,
            )
            .order_by(Message.created_at, Message.id)
        )
        turns: list[ConversationTurn] = []
        for message in rows:
            text = str(message.content_json.get("text", ""))
            if not text:
                continue
            role = "agent" if message.sender_type == SenderType.AGENT.value else "user"
            # The initial user message often duplicates the task description;
            # skip it so the prompt does not repeat itself.
            if not turns and role == "user" and text.strip() == task.description.strip():
                continue
            turns.append(ConversationTurn(role=role, text=text))
        return tuple(turns)

    @activity.defn(name=ACTIVITY_FINALIZE_RUN)
    async def finalize_run_activity(self, params: FinalizeInput) -> None:
        workspace_id = UUID(params.workspace_id)
        task_id = UUID(params.task_id)
        error_message = redact_text(params.error_message) if params.error_message else None
        run_totals: dict[str, Any] = {}

        async with self._resources.session_factory() as session:
            if params.run_id is not None:
                run = await session.get(AgentRun, UUID(params.run_id))
                if run is not None and run.workspace_id == workspace_id:
                    run.status = params.status
                    run.completed_at = datetime.now(UTC)
                    run.steps_used = params.steps_used
                    run.error_code = params.error_code
                    run.error_message = error_message
                    run_totals = {
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "cost_micros": run.estimated_cost_micros,
                    }
                    seq = await self._next_seq(session, run.id)
                    self._add_run_event(
                        session,
                        workspace_id=workspace_id,
                        run_id=run.id,
                        task_id=task_id,
                        seq=seq,
                        event_type=f"run.{params.status}",
                        payload={
                            **run_totals,
                            "steps_used": params.steps_used,
                            "error_code": params.error_code,
                            "error_message": error_message,
                        },
                    )

            task = await session.scalar(
                select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
            )
            if task is not None:
                task.state = params.status
                if error_message:
                    session.add(
                        Message(
                            workspace_id=workspace_id,
                            task_id=task_id,
                            run_id=UUID(params.run_id) if params.run_id else None,
                            sender_type=SenderType.SYSTEM.value,
                            sender_id=None,
                            recipient_type=RecipientType.TASK.value,
                            recipient_id=task_id,
                            message_type="error",
                            content_json={
                                "text": f"Run {params.status}: {error_message}",
                                "error_code": params.error_code,
                            },
                            visibility=MessageVisibility.VISIBLE.value,
                        )
                    )
            await session.commit()

        await self._publish(
            workspace_id,
            f"agent.run.{params.status}",
            {
                "run_id": params.run_id,
                "task_id": params.task_id,
                "error_code": params.error_code,
                **run_totals,
            },
        )
        await self._publish(
            workspace_id,
            f"task.{params.status}",
            {"task_id": params.task_id, "run_id": params.run_id},
        )
