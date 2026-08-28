"""Routes for tasks, runs, and agent messaging (plan 19).

/api/v1/workspaces/{workspace_id}/tasks           create/list/detail + signals
/api/v1/workspaces/{workspace_id}/runs            run list/detail + timeline
/api/v1/workspaces/{workspace_id}/agents/{id}/... assign-task, message
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import DbSession, MemberCtx, TemporalDep, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.tasks import service
from jhin_api.tasks.schemas import (
    AgentMessageIn,
    InstructionIn,
    MessageOut,
    RunEventOut,
    RunListOut,
    RunOut,
    TaskAssign,
    TaskCreate,
    TaskDetailOut,
    TaskListOut,
    TaskOut,
    TaskTreeNodeOut,
    TaskTreeOut,
    ToolCallOut,
)

tasks_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/tasks",
    tags=["tasks"],
    dependencies=[Depends(csrf_protect)],
)
runs_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/runs",
    tags=["runs"],
    dependencies=[Depends(csrf_protect)],
)
agent_actions_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/agents/{agent_id}",
    tags=["tasks"],
    dependencies=[Depends(csrf_protect)],
)


# --- Tasks ---


@tasks_router.post("", status_code=201)
async def create_task(
    payload: TaskCreate, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> TaskOut:
    task = await service.create_task(
        db,
        ctx,
        temporal,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


@tasks_router.get("")
async def list_tasks(
    ctx: ViewerCtx,
    db: DbSession,
    state: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TaskListOut:
    items, total = await service.list_tasks(
        db, ctx.workspace_id, state=state, agent_id=agent_id, limit=limit, offset=offset
    )
    return TaskListOut(items=[TaskOut.model_validate(t) for t in items], total=total)


@tasks_router.get("/{task_id}")
async def get_task(task_id: UUID, ctx: ViewerCtx, db: DbSession) -> TaskDetailOut:
    task = await service.get_task(db, ctx.workspace_id, task_id)
    runs = await service.list_task_runs(db, ctx.workspace_id, task_id)
    return TaskDetailOut(
        task=TaskOut.model_validate(task),
        runs=[RunOut.model_validate(r) for r in runs],
        total_input_tokens=sum(r.input_tokens for r in runs),
        total_output_tokens=sum(r.output_tokens for r in runs),
        total_cost_micros=sum(r.estimated_cost_micros for r in runs),
    )


@tasks_router.get("/{task_id}/tree")
async def task_tree(task_id: UUID, ctx: ViewerCtx, db: DbSession) -> TaskTreeOut:
    """Delegation chain around a task: lineage root plus every descendant."""
    root, tasks = await service.get_task_tree(db, ctx.workspace_id, task_id)
    task_ids = [t.id for t in tasks]
    run_status = await service.latest_run_status_by_task(db, ctx.workspace_id, task_ids)
    names = await service.agent_names(
        db,
        ctx.workspace_id,
        [t.assigned_agent_id for t in tasks if t.assigned_agent_id is not None],
    )

    nodes: dict[UUID, TaskTreeNodeOut] = {
        t.id: TaskTreeNodeOut(
            task=TaskOut.model_validate(t),
            agent_name=names.get(t.assigned_agent_id) if t.assigned_agent_id else None,
            latest_run_status=run_status.get(t.id),
        )
        for t in tasks
    }
    for t in tasks:
        if t.parent_task_id is not None and t.parent_task_id in nodes and t.id != root.id:
            nodes[t.parent_task_id].children.append(nodes[t.id])
    return TaskTreeOut(root=nodes[root.id], focus_task_id=task_id)


@tasks_router.get("/{task_id}/timeline")
async def task_timeline(task_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[RunEventOut]:
    await service.get_task(db, ctx.workspace_id, task_id)
    events = await service.list_task_events(db, ctx.workspace_id, task_id)
    return [RunEventOut.model_validate(e) for e in events]


@tasks_router.get("/{task_id}/messages")
async def task_messages(task_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[MessageOut]:
    await service.get_task(db, ctx.workspace_id, task_id)
    messages = await service.list_task_messages(db, ctx.workspace_id, task_id)
    return [MessageOut.model_validate(m) for m in messages]


@tasks_router.post("/{task_id}/pause")
async def pause_task(
    task_id: UUID, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> TaskOut:
    task = await service.signal_task(
        db,
        ctx,
        temporal,
        task_id,
        signal="pause",
        action="task.paused",
        # Deliberately no state write. The run pauses between steps, and one
        # that never reaches another step boundary -- a single long generation,
        # the case where pausing matters most -- cannot honour it. Claiming
        # "paused" here showed a stopped task and a Resume button for a run
        # that carried on and billed. The workflow records the pause it is
        # actually observing.
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


@tasks_router.post("/{task_id}/resume")
async def resume_task(
    task_id: UUID, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> TaskOut:
    task = await service.signal_task(
        db,
        ctx,
        temporal,
        task_id,
        signal="resume",
        action="task.resumed",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


@tasks_router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: UUID, request: Request, ctx: MemberCtx, db: DbSession, temporal: TemporalDep
) -> TaskOut:
    task = await service.signal_task(
        db,
        ctx,
        temporal,
        task_id,
        signal="cancel",
        action="task.cancel_requested",
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


@tasks_router.post("/{task_id}/acknowledge")
async def acknowledge_task(
    task_id: UUID, request: Request, ctx: MemberCtx, db: DbSession
) -> TaskOut:
    """Dismiss a failed task from the attention inbox (the failure is kept)."""
    task = await service.acknowledge_task(
        db, ctx, task_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return TaskOut.model_validate(task)


@tasks_router.post("/{task_id}/instruction")
async def task_instruction(
    task_id: UUID,
    payload: InstructionIn,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> TaskOut:
    task = await service.send_instruction(
        db,
        ctx,
        temporal,
        task_id,
        text=payload.text,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


# --- Runs ---


@runs_router.get("")
async def list_runs(
    ctx: ViewerCtx,
    db: DbSession,
    status: str | None = None,
    agent_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> RunListOut:
    items, total = await service.list_runs(
        db, ctx.workspace_id, status_filter=status, agent_id=agent_id, limit=limit, offset=offset
    )
    return RunListOut(items=[RunOut.model_validate(r) for r in items], total=total)


@runs_router.get("/{run_id}")
async def get_run(run_id: UUID, ctx: ViewerCtx, db: DbSession) -> RunOut:
    return RunOut.model_validate(await service.get_run(db, ctx.workspace_id, run_id))


@runs_router.get("/{run_id}/timeline")
async def run_timeline(run_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[RunEventOut]:
    await service.get_run(db, ctx.workspace_id, run_id)
    events = await service.list_run_events(db, ctx.workspace_id, run_id)
    return [RunEventOut.model_validate(e) for e in events]


@runs_router.get("/{run_id}/tool-calls")
async def run_tool_calls(run_id: UUID, ctx: ViewerCtx, db: DbSession) -> list[ToolCallOut]:
    await service.get_run(db, ctx.workspace_id, run_id)
    calls = await service.list_run_tool_calls(db, ctx.workspace_id, run_id)
    return [ToolCallOut.model_validate(c) for c in calls]


# --- Agent actions ---


@agent_actions_router.post("/assign-task", status_code=201)
async def assign_task(
    agent_id: UUID,
    payload: TaskAssign,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> TaskOut:
    task = await service.assign_task(
        db,
        ctx,
        temporal,
        agent_id,
        values=payload.model_dump(),
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)


@agent_actions_router.post("/message", status_code=201)
async def message_agent(
    agent_id: UUID,
    payload: AgentMessageIn,
    request: Request,
    ctx: MemberCtx,
    db: DbSession,
    temporal: TemporalDep,
) -> TaskOut:
    task = await service.message_agent(
        db,
        ctx,
        temporal,
        agent_id,
        text=payload.text,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
    return TaskOut.model_validate(task)
