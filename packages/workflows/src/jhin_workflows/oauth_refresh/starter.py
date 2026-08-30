"""Best-effort starter for a workspace's OAuth refresher.

Called after every successful authorization. Idempotent by workflow id, so
the caller neither knows nor cares whether the refresher is already running,
and never raises — failing to start a background refresher must not fail the
connection the user just made.
"""

from __future__ import annotations

from typing import Literal

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from jhin_observability import get_logger
from jhin_workflows.oauth_refresh.shared import (
    OAUTH_REFRESH_WORKFLOW,
    SIGNAL_OAUTH_REFRESH_NOW,
    OAuthRefreshInput,
    OAuthRefreshResult,
    oauth_refresh_workflow_id,
)
from jhin_workflows.task_queues import AGENT_TASK_QUEUE

logger = get_logger(__name__)

StartStatus = Literal["started", "running", "failed"]


async def ensure_oauth_refresh(
    client: Client,
    params: OAuthRefreshInput,
    *,
    task_queue: str = AGENT_TASK_QUEUE,
) -> tuple[StartStatus, WorkflowHandle[object, OAuthRefreshResult] | None]:
    """Start the workspace's refresher, or nudge the one already running."""
    workflow_id = oauth_refresh_workflow_id(params.workspace_id)
    try:
        handle = await client.start_workflow(
            OAUTH_REFRESH_WORKFLOW,
            params,
            id=workflow_id,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=OAuthRefreshResult,
        )
    except WorkflowAlreadyStartedError:
        try:
            await client.get_workflow_handle(workflow_id).signal(SIGNAL_OAUTH_REFRESH_NOW)
        except Exception as exc:
            logger.warning("oauth.refresh_signal_failed", error_type=type(exc).__name__)
        return "running", None
    except Exception as exc:
        logger.warning("oauth.refresh_start_failed", error_type=type(exc).__name__)
        return "failed", None
    return "started", handle
