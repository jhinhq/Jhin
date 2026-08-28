"""Agent worker: registers AgentTaskWorkflow + agent activities on the
dedicated agent task queue (plan architecture: separate worker so model and
secret dependencies never enter the general workflow worker or the API).
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from types import TracebackType
from typing import Any

from temporalio.client import Client

from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.compatibility import AgentCompatibilityActivities
from jhin_agent_worker.coordination_activities import CoordinationActivities
from jhin_agent_worker.engineering_activities import EngineeringActivities
from jhin_agent_worker.media_activities import MediaActivities
from jhin_agent_worker.memory_activities import MemoryActivities
from jhin_agent_worker.resources import Resources
from jhin_agent_worker.settings import Settings
from jhin_agent_worker.trigger_activities import (
    TriggerActivities,
    TriggerCompatibilityActivities,
)
from jhin_observability import (
    ObservabilityRuntime,
    build_temporal_worker,
    connect_temporal_client,
    get_logger,
    initialize_observability,
    service_version,
)
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat
from jhin_secrets.redaction import redact_event_dict
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.avatar_generation import AvatarGenerationWorkflow
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.memory_maintenance import MemoryMaintenanceWorkflow
from jhin_workflows.periodic_review import PeriodicReviewWorkflow
from jhin_workflows.triggered_task import TriggeredTaskWorkflow
from jhin_workflows.work_request_task import WorkRequestTaskWorkflow

logger = get_logger(__name__)


async def connect_with_retry(
    settings: Settings,
    runtime: ObservabilityRuntime,
) -> Client:
    delay = 1.0
    while True:
        try:
            return await connect_temporal_client(settings, runtime)
        except Exception as exc:
            logger.warning(
                "temporal.connect_retry",
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def resources_with_retry(
    settings: Settings,
    runtime: ObservabilityRuntime,
) -> Resources:
    delay = 1.0
    while True:
        try:
            return await Resources.create(settings, runtime=runtime)
        except Exception as exc:
            logger.warning(
                "resources.retry",
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def _cleanup_process(
    *,
    loop: asyncio.AbstractEventLoop | None,
    registered_signals: list[signal.Signals],
    worker: Any | None,
    worker_exit_needed: bool,
    heartbeat_task: asyncio.Task[None] | None,
    resources: Resources | None,
    runtime: ObservabilityRuntime,
    active_error: BaseException | None,
    active_traceback: TracebackType | None,
) -> BaseException | None:
    first_cancellation: asyncio.CancelledError | None = None
    first_error: BaseException | None = None

    def remember(error: BaseException) -> None:
        nonlocal first_cancellation, first_error
        if isinstance(error, asyncio.CancelledError):
            if first_cancellation is None:
                first_cancellation = error
        elif first_error is None:
            first_error = error

    if loop is not None:
        for handled_signal in registered_signals:
            try:
                loop.remove_signal_handler(handled_signal)
            except BaseException as error:
                remember(error)
    if worker is not None and worker_exit_needed:
        try:
            await worker.__aexit__(
                type(active_error) if active_error is not None else None,
                active_error,
                active_traceback,
            )
        except BaseException as error:
            remember(error)
    if heartbeat_task is not None:
        cancel_succeeded = False
        try:
            heartbeat_task.cancel()
            cancel_succeeded = True
        except BaseException as error:
            remember(error)
        try:
            await heartbeat_task
        except asyncio.CancelledError as error:
            if not cancel_succeeded:
                remember(error)
        except BaseException as error:
            remember(error)
    try:
        clear_heartbeat()
    except BaseException as error:
        remember(error)
    if resources is not None:
        try:
            await resources.close()
        except BaseException as error:
            remember(error)
    try:
        runtime.shutdown(timeout_millis=5_000)
    except BaseException as error:
        remember(error)
    return first_cancellation or first_error


async def _await_cleanup(
    task: asyncio.Task[BaseException | None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
            if task.done():
                return task.result(), cancellation


async def main() -> None:
    settings = Settings()
    resources: Resources | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    worker: Any | None = None
    worker_exit_needed = False
    loop: asyncio.AbstractEventLoop | None = None
    registered_signals: list[signal.Signals] = []
    active_error: BaseException | None = None
    active_traceback: TracebackType | None = None
    runtime = initialize_observability(
        settings.observability_config(
            service_name="agent-worker",
            service_version=service_version("jhin-agent-worker"),
            extra_log_processors=(redact_event_dict,),
        )
    )
    try:
        client = await connect_with_retry(settings, runtime)
        resources = await resources_with_retry(settings, runtime)
        activities = AgentActivities(resources, temporal_client=client)
        compatibility = AgentCompatibilityActivities(resources, client)
        trigger_activities = TriggerActivities(resources)
        trigger_compatibility = TriggerCompatibilityActivities(client)
        engineering_activities = EngineeringActivities(resources)
        memory_activities = MemoryActivities(resources)
        coordination_activities = CoordinationActivities(resources, temporal_client=client)
        media_activities = MediaActivities(resources)
        logger.info(
            "temporal.connected",
            task_queue=AGENT_TASK_QUEUE,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            registered_signals.append(sig)

        agent_workflows = [
            AgentTaskWorkflow,
            TriggeredTaskWorkflow,
            DelegatedTaskWorkflow,
            EngineeringTicketWorkflow,
            AvatarGenerationWorkflow,
            WorkRequestTaskWorkflow,
            MemoryMaintenanceWorkflow,
            PeriodicReviewWorkflow,
        ]
        agent_activities: list[Callable[..., Any]] = [
            activities.resolve_snapshot_activity,
            activities.reason_agent_step_activity,
            activities.commit_agent_step_activity,
            activities.commit_approval_projection_activity,
            activities.commit_review_projection_activity,
            activities.finalize_run_projection_activity,
            activities.summarize_delegation_activity,
            activities.deliver_delegation_result_activity,
            activities.deliver_question_answer_activity,
            compatibility.run_agent_step_activity,
            compatibility.resolve_approval_activity,
            compatibility.finalize_run_activity,
            trigger_activities.prepare_triggered_task_activity,
            trigger_compatibility.sync_external_activity,
            engineering_activities.resolve_engineering_plan_activity,
            engineering_activities.create_engineering_child_task_activity,
            engineering_activities.finalize_engineering_ticket_activity,
            memory_activities.extract_memory_candidates_activity,
            memory_activities.apply_memory_candidates_activity,
            media_activities.generate_avatar_activity,
            media_activities.fail_avatar_generation_activity,
            coordination_activities.finalize_work_request_activity,
            coordination_activities.note_work_request_unanswered_activity,
            coordination_activities.mark_task_paused_activity,
            coordination_activities.load_periodic_review_policy_activity,
            coordination_activities.open_periodic_review_activity,
        ]
        heartbeat_task = asyncio.create_task(run_heartbeat())
        worker = build_temporal_worker(
            client,
            runtime=runtime,
            task_queue=AGENT_TASK_QUEUE,
            workflows=agent_workflows,
            activities=agent_activities,
        )
        await worker.__aenter__()
        worker_exit_needed = True
        logger.info("worker.started", task_queue=AGENT_TASK_QUEUE)
        await stop.wait()
        logger.info("worker.stopping")
    except BaseException as error:
        active_error = error
        active_traceback = error.__traceback__

    cleanup_task = asyncio.create_task(
        _cleanup_process(
            loop=loop,
            registered_signals=registered_signals,
            worker=worker,
            worker_exit_needed=worker_exit_needed,
            heartbeat_task=heartbeat_task,
            resources=resources,
            runtime=runtime,
            active_error=active_error,
            active_traceback=active_traceback,
        ),
        name="agent-worker-cleanup",
    )
    cleanup_error, cleanup_cancellation = await _await_cleanup(cleanup_task)
    if isinstance(active_error, asyncio.CancelledError):
        raise active_error.with_traceback(active_traceback)
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    if active_error is not None:
        raise active_error.with_traceback(active_traceback)
    if cleanup_error is not None:
        raise cleanup_error


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
