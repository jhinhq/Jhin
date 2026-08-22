"""Best-effort starter the API and worker call after visible work."""

from __future__ import annotations

from typing import Literal

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_observability import get_logger
from jhin_workflows.memory_maintenance.shared import (
    MEMORY_MAINTENANCE_WORKFLOW,
    SOURCE_KINDS,
    MemoryMaintenanceInput,
    MemoryMaintenanceResult,
    memory_maintenance_workflow_id,
)
from jhin_workflows.task_queues import AGENT_TASK_QUEUE

logger = get_logger(__name__)

StartStatus = Literal["started", "duplicate", "invalid", "failed"]


async def start_memory_maintenance(
    client: Client,
    params: MemoryMaintenanceInput,
    *,
    task_queue: str = AGENT_TASK_QUEUE,
) -> tuple[StartStatus, WorkflowHandle[object, MemoryMaintenanceResult] | None]:
    """Start the deterministic maintenance workflow; never raises.

    Idempotent by (source_kind, source_id, turn_marker): a second start for
    the same source returns ``"duplicate"``. Any other failure returns
    ``"failed"`` so the caller's chat turn / task completion proceeds.
    """
    if params.source_kind not in SOURCE_KINDS or not params.source_id:
        return "invalid", None
    workflow_id = memory_maintenance_workflow_id(params)
    try:
        handle = await client.start_workflow(
            MEMORY_MAINTENANCE_WORKFLOW,
            params,
            id=workflow_id,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            result_type=MemoryMaintenanceResult,
        )
    except WorkflowAlreadyStartedError:
        return "duplicate", None
    except Exception as exc:
        logger.warning("memory.maintenance_start_failed", error_type=type(exc).__name__)
        return "failed", None
    return "started", handle
