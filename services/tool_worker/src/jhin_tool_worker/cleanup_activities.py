"""Idempotent best-effort sandbox workspace cleanup on the tool worker.

Finalize used to mean "destroy the volume", because every volume belonged to
one run. Now a run may be holding a *durable* workspace that belongs to the
agent, so finalize means "give back what this run held":

* the agent's durable workspace is **released** — the holder is cleared, the
  last holder is remembered so a run that lost its lease can be told so, and
  no runner call is made at all. The disk survives for the agent's next turn,
  which is the entire point of it;
* a private ``run-<id>`` workspace is **destroyed**, exactly as before;
* a run with no binding at all — one that never touched the sandbox, or one
  that started before durable workspaces existed — falls back to the legacy
  ``DELETE /v1/workspaces/run-<run_id>``, which is idempotent: a volume that
  was never created is 204, the same answer as one that was removed, and only
  a volume Docker refused to remove is a conflict.

Cleanup stays best-effort by contract, which is why nothing in the bind path
depends on it having run: a lease is taken from a holder whose own run row says
it is finished, not from one whose cleanup happened to succeed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jhin_connectors.cli.runner_client import delete_workspace as delete_sandbox_workspace
from jhin_connectors.cli.workspace import release_run_bindings, run_workspace_key
from jhin_db.models import AgentRun, SandboxWorkspace
from jhin_tool_worker.drain import WorkerDrain
from jhin_tool_worker.resources import ToolWorkerResources
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
        resources: ToolWorkerResources,
        *,
        delete_workspace: DeleteWorkspace = delete_sandbox_workspace,
        drain: WorkerDrain | None = None,
    ) -> None:
        self._resources = resources
        self._delete_workspace = delete_workspace
        self._attempted: set[UUID] = set()
        self._lock = asyncio.Lock()
        # A worker with no drain (unit tests, direct callers) behaves exactly
        # as it did: a fresh drain is never draining.
        self._drain = drain if drain is not None else WorkerDrain()

    @activity.defn(name=ACTIVITY_CLEANUP_RUN_WORKSPACE)
    async def cleanup_run_workspace_activity(
        self,
        params: CleanupRunWorkspaceInput,
    ) -> CleanupRunWorkspaceResult:
        # Drained with the rest of this queue. Cleanup is idempotent and
        # best-effort, so an abandoned one costs nobody an outcome — but it
        # is also the activity that remembers, in this process, that it has
        # already attempted a run. A worker that takes one on its way out
        # marks the run attempted and then dies before releasing the lease,
        # and the retry that would have released it lands on a *different*
        # process with an empty memory only by luck. Refusing before the mark
        # keeps that memory honest.
        with self._drain.hold():
            return await self._cleanup_run_workspace(params)

    async def _cleanup_run_workspace(
        self,
        params: CleanupRunWorkspaceInput,
    ) -> CleanupRunWorkspaceResult:
        try:
            workspace_id = UUID(params.workspace_id)
            run_id = UUID(params.run_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ApplicationError(
                "cleanup workspace or run identity is invalid",
                type="cleanup_identity_invalid",
                non_retryable=True,
            ) from error

        async with self._resources.session_factory() as session:
            bound_run_id = await session.scalar(
                select(AgentRun.id).where(
                    AgentRun.id == run_id,
                    AgentRun.workspace_id == workspace_id,
                )
            )
        if bound_run_id is None:
            raise ApplicationError(
                "cleanup run is not bound to the requested workspace",
                type="cleanup_context_invalid",
                non_retryable=True,
            )

        async with self._lock:
            if run_id in self._attempted:
                return CleanupRunWorkspaceResult(deleted=False)
            self._attempted.add(run_id)
            try:
                deleted = await self._release(workspace_id, run_id)
            except Exception:
                deleted = False
            return CleanupRunWorkspaceResult(deleted=bool(deleted))

    async def _release(self, workspace_id: UUID, run_id: UUID) -> bool:
        """Give back this run's binding, or fall back to the legacy delete.

        The fallback is what keeps a run that predates durable workspaces — or
        one that never made a sandbox call — behaving exactly as it did: the
        runner's DELETE is idempotent and answers "already gone" the same way
        it answers "removed", so asking for a volume that was never created
        costs one request and nothing else.
        """
        async with self._resources.session_factory() as session:
            bound = await session.scalar(
                select(SandboxWorkspace.id).where(
                    SandboxWorkspace.workspace_id == workspace_id,
                    SandboxWorkspace.holder_run_id == run_id,
                )
            )
        if bound is None:
            return bool(await self._delete_workspace(run_workspace_key(run_id)))
        return await release_run_bindings(
            self._resources.session_factory,
            workspace_id=workspace_id,
            run_id=run_id,
            delete_workspace=self._delete_workspace,
        )


__all__ = ["CleanupActivities", "DeleteWorkspace"]
