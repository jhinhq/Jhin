"""Agent worker: registers AgentTaskWorkflow + agent activities on the
dedicated agent task queue (plan architecture: separate worker so model and
secret dependencies never enter the general workflow worker or the API).
"""

from __future__ import annotations

import asyncio
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.coordination_activities import CoordinationActivities
from jhin_agent_worker.engineering_activities import EngineeringActivities
from jhin_agent_worker.media_activities import MediaActivities
from jhin_agent_worker.memory_activities import MemoryActivities
from jhin_agent_worker.resources import Resources
from jhin_agent_worker.settings import Settings
from jhin_agent_worker.trigger_activities import TriggerActivities
from jhin_observability import configure_logging, get_logger
from jhin_observability.healthfile import clear_heartbeat, run_heartbeat
from jhin_secrets.redaction import redact_event_dict
from jhin_workflows import AGENT_TASK_QUEUE
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.avatar_generation import AvatarGenerationWorkflow
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.memory_maintenance import MemoryMaintenanceWorkflow
from jhin_workflows.triggered_task import TriggeredTaskWorkflow
from jhin_workflows.work_request_task import WorkRequestTaskWorkflow

logger = get_logger(__name__)


async def connect_with_retry(settings: Settings) -> Client:
    delay = 1.0
    while True:
        try:
            return await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
        except Exception as exc:
            logger.warning(
                "temporal.connect_retry",
                address=settings.temporal_address,
                error=f"{type(exc).__name__}: {exc}"[:200],
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def resources_with_retry(settings: Settings) -> Resources:
    delay = 1.0
    while True:
        try:
            return await Resources.create(settings)
        except Exception as exc:
            logger.warning(
                "resources.retry",
                error=f"{type(exc).__name__}: {exc}"[:200],
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def main() -> None:
    settings = Settings()
    # This worker decrypts credentials: redact known secrets from every log.
    configure_logging("agent-worker", settings.log_level, extra_processors=[redact_event_dict])

    client = await connect_with_retry(settings)
    resources = await resources_with_retry(settings)
    activities = AgentActivities(resources, temporal_client=client)
    trigger_activities = TriggerActivities(resources)
    engineering_activities = EngineeringActivities(resources)
    memory_activities = MemoryActivities(resources)
    coordination_activities = CoordinationActivities(resources)
    media_activities = MediaActivities(resources)
    logger.info(
        "temporal.connected",
        address=settings.temporal_address,
        namespace=settings.temporal_namespace,
        task_queue=AGENT_TASK_QUEUE,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    heartbeat_task = asyncio.create_task(run_heartbeat())
    worker = Worker(
        client,
        task_queue=AGENT_TASK_QUEUE,
        workflows=[
            AgentTaskWorkflow,
            TriggeredTaskWorkflow,
            DelegatedTaskWorkflow,
            EngineeringTicketWorkflow,
            AvatarGenerationWorkflow,
            WorkRequestTaskWorkflow,
            MemoryMaintenanceWorkflow,
        ],
        activities=[
            activities.resolve_snapshot_activity,
            activities.run_agent_step_activity,
            activities.resolve_approval_activity,
            activities.finalize_run_activity,
            activities.summarize_delegation_activity,
            activities.deliver_delegation_result_activity,
            trigger_activities.prepare_triggered_task_activity,
            trigger_activities.sync_external_activity,
            engineering_activities.resolve_engineering_plan_activity,
            engineering_activities.create_engineering_child_task_activity,
            engineering_activities.finalize_engineering_ticket_activity,
            memory_activities.extract_memory_candidates_activity,
            memory_activities.apply_memory_candidates_activity,
            media_activities.generate_avatar_activity,
            media_activities.fail_avatar_generation_activity,
            coordination_activities.finalize_work_request_activity,
        ],
    )
    try:
        async with worker:
            logger.info("worker.started", task_queue=AGENT_TASK_QUEUE)
            await stop.wait()
            logger.info("worker.stopping")
    finally:
        heartbeat_task.cancel()
        clear_heartbeat()
        await resources.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
