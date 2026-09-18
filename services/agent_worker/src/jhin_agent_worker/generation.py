"""Public draft attempts, with bounded safe snapshots and prompt cancellation."""

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from temporalio.exceptions import ApplicationError

from jhin_agent_worker.resources import Resources
from jhin_agents.context import TaskContext
from jhin_agents.runtime import StepOutcome, execute_step
from jhin_agents.snapshot import AgentExecutionSnapshot
from jhin_db.models import Task
from jhin_db.models.timeline import ModelGeneration
from jhin_domain import new_uuid7
from jhin_models import ModelClient, ModelStreamEvent, ToolSchema
from jhin_secrets.redaction import get_redactor
from jhin_tools.sanitize import sanitize_payload
from jhin_workflows.agent_task.shared import ReasonAgentStepInput


def _reject_truncated_completion(outcome: StepOutcome) -> None:
    # A partial answer is not task completion. Fail without retrying the model
    # or repeating work already performed by earlier tool steps.
    if outcome.finish_reason == "length" and not outcome.tool_calls:
        raise ApplicationError(
            "The model response reached its output limit before finishing; the task is incomplete.",
            type="model_output_truncated",
            non_retryable=True,
        )


async def execute_public_generation(
    resources: Resources,
    client: Any,
    snapshot: AgentExecutionSnapshot,
    context: TaskContext,
    task: Task,
    params: ReasonAgentStepInput,
    tools: tuple[ToolSchema, ...],
    *,
    nudge: str = "",
    defer_text_until_tool_calls: bool = False,
) -> StepOutcome:
    # Compatibility adapters without the typed contract remain single-call.
    if not isinstance(client, ModelClient):
        outcome = await execute_step(client, snapshot, context, tools=tools, nudge=nudge)
        _reject_truncated_completion(outcome)
        return outcome
    identity = new_uuid7()
    async with resources.session_factory() as session:
        current = await session.scalar(
            select(Task.metadata_json).where(
                Task.id == task.id, Task.workspace_id == task.workspace_id
            )
        )
        if current is None or current.get("stop_requested_at"):
            raise ApplicationError(
                "Generation stopped by the user", type="generation_cancelled", non_retryable=True
            )
        await session.execute(
            update(ModelGeneration)
            .where(
                ModelGeneration.run_id == UUID(params.run_id),
                ModelGeneration.step == params.step_index,
                ModelGeneration.workspace_id == task.workspace_id,
                ModelGeneration.status.in_(("running", "completed")),
            )
            .values(status="superseded", completed_at=datetime.now(UTC))
        )
        session.add(
            ModelGeneration(
                id=identity,
                workspace_id=task.workspace_id,
                conversation_id=task.conversation_id,
                task_id=task.id,
                run_id=UUID(params.run_id),
                agent_id=UUID(params.agent_id),
                step=params.step_index,
                model=snapshot.model_profile.model_name,
            )
        )
        await session.commit()
    raw = ""
    publish_text = not defer_text_until_tool_calls
    last = 0.0
    metadata: dict[str, Any] = {}

    async def persist(status: str = "running", *, force: bool = False) -> None:
        nonlocal last
        now = time.monotonic()
        if not force and now - last < 0.4:
            return
        last = now
        redactor = get_redactor()
        # Always sanitize the complete accumulated prefix. At each published
        # edge redact unfinished secret prefixes so a split chunk cannot leak.
        safe = redactor.redact_partial_text(raw, clipped_start=False) if publish_text else ""
        metadata["text_truncated"] = len(safe) > 65_536
        async with resources.session_factory() as session:
            await session.execute(
                update(ModelGeneration)
                .where(
                    ModelGeneration.id == identity,
                    ModelGeneration.workspace_id == task.workspace_id,
                    ModelGeneration.status == "running",
                )
                .values(
                    text=safe[:65_536],
                    status=status,
                    metadata_json=sanitize_payload(metadata),
                    completed_at=datetime.now(UTC) if status != "running" else None,
                )
            )
            await session.commit()
        if task.conversation_id is not None:
            from jhin_events import EventEnvelope, EventSource

            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    resources.publisher.publish(
                        EventEnvelope(
                            workspace_id=str(task.workspace_id),
                            event_type="conversation.item.changed",
                            source=EventSource(type="agent_worker"),
                            data={
                                "conversation_id": str(task.conversation_id),
                                "generation_id": str(identity),
                            },
                        )
                    ),
                    timeout=0.2,
                )

    async def observe(event: ModelStreamEvent) -> None:
        nonlocal raw, publish_text
        if event.type == "text_delta":
            raw += event.text
        elif event.type == "citation":
            metadata.setdefault("citations", []).append(event.data)
        elif event.type == "usage":
            metadata["usage"] = event.data
        elif event.type == "completed" and event.response is not None:
            raw = event.response.text
            # A tool-free candidate will be reviewed before it becomes an
            # answer. Keep it out of every public snapshot, including its
            # completed row. Once actual calls are known, its tool-step
            # commentary is public and will not be replaced by that review.
            publish_text = publish_text or bool(event.response.tool_calls)
            metadata.update(
                usage=event.response.usage.model_dump(), finish_reason=event.response.finish_reason
            )
        await persist()

    async def cancellation_requested() -> None:
        while True:
            await asyncio.sleep(0.5)
            async with resources.session_factory() as session:
                metadata = await session.scalar(
                    select(Task.metadata_json).where(
                        Task.id == task.id, Task.workspace_id == task.workspace_id
                    )
                )
            if metadata is None or metadata.get("stop_requested_at"):
                return

    generation = asyncio.create_task(
        execute_step(client, snapshot, context, tools=tools, nudge=nudge, on_event=observe)
    )
    cancelled = asyncio.create_task(cancellation_requested())
    try:
        done, _ = await asyncio.wait((generation, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done:
            await cancelled
            generation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await generation
            await persist("cancelled", force=True)
            raise ApplicationError(
                "Generation stopped by the user", type="generation_cancelled", non_retryable=True
            )
        outcome = await generation
        _reject_truncated_completion(outcome)
        await persist("completed", force=True)
        return outcome
    except BaseException:
        generation.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await generation
        with contextlib.suppress(Exception):
            await persist("cancelled" if generation.cancelled() else "failed", force=True)
        raise
    finally:
        cancelled.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancelled
