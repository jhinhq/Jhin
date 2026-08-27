"""Trigger routes (plan 10.3, 17.10).

Reads are member-level (the triggers page); writes are admin-only, matching
connections — a trigger grants standing authority to start agent work.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from jhin_api.deps import AdminCtx, DbSession, ViewerCtx
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect
from jhin_api.triggers import service
from jhin_api.triggers.schemas import (
    TriggerCreate,
    TriggerInvocationOut,
    TriggerOut,
    TriggerTestRequest,
    TriggerTestResult,
    TriggerUpdate,
)
from jhin_db.models import Trigger, TriggerInvocation

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/triggers",
    tags=["triggers"],
    dependencies=[Depends(csrf_protect)],
)


def _invocation_out(
    invocation: TriggerInvocation, health: service.TargetHealth | None = None
) -> TriggerInvocationOut:
    result = TriggerInvocationOut.model_validate(invocation, from_attributes=True)
    result.error_message = service.invocation_message(invocation, health)
    return result


def _out(
    trigger: Trigger,
    last: TriggerInvocation | None = None,
    health: service.TargetHealth | None = None,
) -> TriggerOut:
    result = TriggerOut.model_validate(trigger, from_attributes=True)
    if last is not None:
        result.last_invocation = _invocation_out(last, health)
    if health is not None:
        result.target_state = health.state
        result.target_warning = health.warning
    return result


@router.get("")
async def list_triggers(ctx: ViewerCtx, db: DbSession) -> list[TriggerOut]:
    triggers = await service.list_triggers(db, ctx.workspace_id)
    # Reconciling here is what keeps a trigger whose agent was deleted from
    # sitting enabled and failing every matching event until someone notices.
    health = await service.reconcile_targets(db, ctx.workspace_id, triggers)
    latest = await service.last_invocations(db, ctx.workspace_id, [t.id for t in triggers])
    return [_out(trigger, latest.get(trigger.id), health.get(trigger.id)) for trigger in triggers]


@router.post("", status_code=201)
async def create_trigger(
    payload: TriggerCreate, request: Request, ctx: AdminCtx, db: DbSession
) -> TriggerOut:
    trigger = await service.create_trigger(
        db, ctx, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(trigger, health=await service.target_health(db, ctx.workspace_id, trigger))


@router.get("/{trigger_id}")
async def get_trigger(trigger_id: UUID, ctx: ViewerCtx, db: DbSession) -> TriggerOut:
    trigger = await service.get_trigger(db, ctx.workspace_id, trigger_id)
    health = await service.reconcile_targets(db, ctx.workspace_id, [trigger])
    latest = await service.last_invocations(db, ctx.workspace_id, [trigger.id])
    return _out(trigger, latest.get(trigger.id), health.get(trigger.id))


@router.patch("/{trigger_id}")
async def update_trigger(
    trigger_id: UUID, payload: TriggerUpdate, request: Request, ctx: AdminCtx, db: DbSession
) -> TriggerOut:
    trigger = await service.update_trigger(
        db, ctx, trigger_id, payload, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(trigger, health=await service.target_health(db, ctx.workspace_id, trigger))


@router.post("/{trigger_id}/enable")
async def enable_trigger(
    trigger_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> TriggerOut:
    trigger = await service.set_enabled(
        db, ctx, trigger_id, enabled=True, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(trigger, health=await service.target_health(db, ctx.workspace_id, trigger))


@router.post("/{trigger_id}/disable")
async def disable_trigger(
    trigger_id: UUID, request: Request, ctx: AdminCtx, db: DbSession
) -> TriggerOut:
    trigger = await service.set_enabled(
        db, ctx, trigger_id, enabled=False, request_id=req_id(request), ip_hash=ip_hash(request)
    )
    return _out(trigger, health=await service.target_health(db, ctx.workspace_id, trigger))


@router.delete("/{trigger_id}", status_code=204)
async def delete_trigger(trigger_id: UUID, request: Request, ctx: AdminCtx, db: DbSession) -> None:
    await service.delete_trigger(
        db, ctx, trigger_id, request_id=req_id(request), ip_hash=ip_hash(request)
    )


@router.post("/{trigger_id}/test")
async def test_trigger(
    trigger_id: UUID, payload: TriggerTestRequest, ctx: AdminCtx, db: DbSession
) -> TriggerTestResult:
    """Dry-run the trigger's filter against a sample event (plan 10.3):
    returns matched/not plus which conditions passed or failed. Never
    records an invocation or starts work."""
    trigger = await service.get_trigger(db, ctx.workspace_id, trigger_id)
    return service.test_trigger(trigger, payload.event)


@router.get("/{trigger_id}/invocations")
async def list_invocations(
    trigger_id: UUID, ctx: ViewerCtx, db: DbSession, limit: int = 20
) -> list[TriggerInvocationOut]:
    trigger = await service.get_trigger(db, ctx.workspace_id, trigger_id)
    health = await service.target_health(db, ctx.workspace_id, trigger)
    rows = await service.list_invocations(db, ctx.workspace_id, trigger_id, limit=limit)
    return [_invocation_out(row, health) for row in rows]
