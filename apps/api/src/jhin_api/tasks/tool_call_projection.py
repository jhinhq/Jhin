"""Public terminal snapshots from tool-worker records, with no runner access."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.tasks.schemas import SandboxJobOut, ToolCallOut
from jhin_db.models import SandboxJob, ToolCall

_OUTPUT_TOOLS = ("cli.command.execute", "cli.test.run")
_TAIL_CHARS = 8192


async def project_tool_calls(
    db: AsyncSession, workspace_id: UUID, calls: Sequence[ToolCall]
) -> list[ToolCallOut]:
    """Use only the latest job bound to this exact workspace, run and call.

    Generated file-tool wrappers contain private evidence trailers; their
    public structured result already carries the readable file operation.
    Live container output is exposed only for actual command/test tools.
    """
    ids = [call.id for call in calls if call.tool_name in _OUTPUT_TOOLS]
    jobs: dict[UUID, SandboxJob] = {}
    if ids:
        ranked = (
            select(
                SandboxJob.id,
                func.row_number()
                .over(
                    partition_by=SandboxJob.tool_call_id,
                    order_by=(SandboxJob.created_at.desc(), SandboxJob.id.desc()),
                )
                .label("rank"),
            )
            .join(
                ToolCall,
                and_(
                    ToolCall.id == SandboxJob.tool_call_id,
                    ToolCall.workspace_id == SandboxJob.workspace_id,
                    ToolCall.run_id == SandboxJob.run_id,
                ),
            )
            .where(SandboxJob.workspace_id == workspace_id, ToolCall.id.in_(ids))
            .subquery()
        )
        rows = await db.scalars(
            select(SandboxJob).join(ranked, ranked.c.id == SandboxJob.id).where(ranked.c.rank == 1)
        )
        jobs = {job.tool_call_id: job for job in rows if job.tool_call_id is not None}
    projected: list[ToolCallOut] = []
    for call in calls:
        if call.workspace_id != workspace_id:
            continue
        item = ToolCallOut.model_validate(call)
        job = jobs.get(call.id)
        if job is not None:
            item.sandbox_job = SandboxJobOut(
                job_id=job.id,
                status=job.status,
                network_policy=job.network_policy,
                stdout=(job.stdout_tail or "")[-_TAIL_CHARS:],
                stderr=(job.stderr_tail or "")[-_TAIL_CHARS:],
                exit_code=job.exit_code,
                started_at=job.started_at,
                completed_at=job.completed_at,
                duration_ms=job.duration_ms,
            )
        projected.append(item)
    return projected
