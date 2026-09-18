"""Ghost network access and credential resolution stay on the tool worker."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from temporalio import activity

from jhin_connectors.ghost.access import ghost_access_allowed
from jhin_connectors.ghost.archive import ARCHIVE_PAGE_SIZE, fetch_page, persist_page
from jhin_connectors.ghost.assignments import require_assignment
from jhin_connectors.ghost.client import GhostApiError, ghost_request
from jhin_connectors.ghost.tools import _api
from jhin_db.models import Agent, AgentCapabilityGrant, Connection, Task
from jhin_db.models.blog_corpus import BlogCorpusSync
from jhin_policy import Grant, GrantEffect
from jhin_tool_worker.resources import ToolWorkerResources
from jhin_tools.builtin import ToolExecutionContext
from jhin_workflows.blog_corpus.shared import (
    ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC,
    BlogCorpusSyncInput,
    BlogCorpusSyncResult,
)


class BlogCorpusActivities:
    def __init__(self, resources: ToolWorkerResources) -> None:
        self._resources = resources

    @activity.defn(name=ACTIVITY_ADVANCE_BLOG_CORPUS_SYNC)
    async def advance(self, params: BlogCorpusSyncInput) -> BlogCorpusSyncResult:
        async with self._resources.session_factory() as session:
            sync = await session.scalar(
                select(BlogCorpusSync)
                .where(
                    BlogCorpusSync.id == UUID(params.sync_id),
                    BlogCorpusSync.workspace_id == UUID(params.workspace_id),
                )
                .with_for_update()
            )
            if sync is None:
                return BlogCorpusSyncResult(params.sync_id, "missing")
            if sync.status in {"queued", "running"}:
                if params.fail:
                    sync.status, sync.active_key, sync.error_code = (
                        "failed",
                        None,
                        "archive_page_failed",
                    )
                else:
                    try:
                        agent = await session.get(Agent, sync.agent_id)
                        connection = await session.get(Connection, sync.connection_id)
                        task = await session.get(Task, sync.task_id)
                        if (
                            task is None
                            or task.state == "cancelled"
                            or task.metadata_json.get("stop_requested_at")
                        ):
                            raise GhostApiError(
                                "Archive task was cancelled", code="ghost_archive_cancelled"
                            )
                        if (
                            agent is None
                            or agent.status != "active"
                            or connection is None
                            or connection.status != "active"
                        ):
                            raise GhostApiError(
                                "Archive actor or connection unavailable",
                                code="ghost_archive_access_denied",
                            )
                        ctx = ToolExecutionContext(
                            session=session,
                            workspace_id=sync.workspace_id,
                            agent_id=sync.agent_id,
                            agent_name=agent.name,
                            task_id=sync.task_id,
                            run_id=sync.run_id,
                            crypto=self._resources.crypto,
                            session_factory=self._resources.session_factory,
                        )
                        grants = [
                            Grant(
                                capability=row.capability,
                                scope=row.scope_json,
                                effect=GrantEffect(row.effect),
                            )
                            for row in await session.scalars(
                                select(AgentCapabilityGrant).where(
                                    AgentCapabilityGrant.workspace_id == sync.workspace_id,
                                    AgentCapabilityGrant.agent_id == sync.agent_id,
                                )
                            )
                        ]
                        if not await ghost_access_allowed(
                            ctx, connection, "ghost.archive.sync", grants
                        ):
                            raise GhostApiError(
                                "Archive access was revoked", code="ghost_archive_access_denied"
                            )
                        await require_assignment(
                            ctx, str(sync.assignment_id), str(sync.connection_id)
                        )
                        _, base, key = await _api(ctx, str(sync.connection_id))

                        async def read(page: int, limit: int) -> dict[str, Any]:
                            return await ghost_request(
                                base,
                                key,
                                "GET",
                                "posts/",
                                params={
                                    "page": page,
                                    # Full articles can be large; avoid also transferring
                                    # the unused Lexical representation on every page.
                                    # The page size is measured against the client's
                                    # 16 MiB bound in ghost/archive.py.
                                    "limit": limit,
                                    "filter": "status:published",
                                    "order": "id asc",
                                    "formats": "html",
                                    "include": "tags,authors",
                                },
                            )

                        # A page the provider cannot deliver under its response bound is
                        # re-read in halves and rejoined, so one outsized article costs a
                        # retry instead of the whole archive sync.
                        payload = await fetch_page(read, sync.next_page, size=ARCHIVE_PAGE_SIZE)
                        await persist_page(session, sync, payload)
                    except GhostApiError as error:
                        if error.status_code == 429 or (
                            error.status_code and error.status_code >= 500
                        ):
                            raise
                        sync.status, sync.active_key, sync.error_code = "failed", None, error.code
            await session.commit()
            return BlogCorpusSyncResult(
                str(sync.id), sync.status, sync.discovered, sync.indexed, sync.failed
            )
