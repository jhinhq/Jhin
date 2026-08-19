"""Idempotent best-effort sandbox workspace cleanup on the tool worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_connectors.cli.runner_client import delete_workspace as delete_sandbox_workspace
from jhin_workflows.agent_task.shared import (
    ACTIVITY_CLEANUP_RUN_WORKSPACE,
    CleanupRunWorkspaceInput,
    CleanupRunWorkspaceResult,
)

DeleteWorkspace = Callable[[str], Awaitable[bool]]


class CleanupActivities:
    """Collapse same-process retries; the runner DELETE remains idempotent."""

    def __init__(
        self,
        *,
        delete_workspace: DeleteWorkspace = delete_sandbox_workspace,
    ) -> None:
        self._delete_workspace = delete_workspace
        self._attempted: set[UUID] = set()
        self._lock = asyncio.Lock()

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace_activity(
        self,
        params: CleanupRunWorkspaceInput,
    ) -> CleanupRunWorkspaceResult:
        try:
            UUID(params.workspace_id)
            run_id = UUID(params.run_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ApplicationError(
                "cleanup workspace or run identity is invalid",
                type="cleanup_identity_invalid",
                non_retryable=True,
            ) from error

        async with self._lock:
            if run_id in self._attempted:
                return CleanupRunWorkspaceResult(deleted=False)
            self._attempted.add(run_id)
            try:
                deleted = await self._delete_workspace(f"run-{run_id}")
            except Exception:
                deleted = False
            return CleanupRunWorkspaceResult(deleted=bool(deleted))


__all__ = ["CleanupActivities", "DeleteWorkspace"]
