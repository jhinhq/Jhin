"""Trigger CRUD, dry-run evaluation, and invocation reads (plan 10.3, 17.10).

Filters are validated with the same pure DSL the event worker evaluates, so
anything accepted here is exactly what will run. All writes are audited.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_api.triggers.schemas import (
    ConditionExplanation,
    TriggerCreate,
    TriggerTestResult,
    TriggerUpdate,
)
from jhin_db.models import Agent, Connection, Team, Trigger, TriggerInvocation
from jhin_domain import TriggerType
from jhin_triggers import FilterError, evaluate_filter, validate_filter


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


async def _validate_references(
    db: AsyncSession,
    workspace_id: UUID,
    *,
    connection_id: UUID | None,
    target_agent_id: UUID | None,
    target_team_id: UUID | None,
) -> None:
    if connection_id is not None:
        connection = await db.scalar(
            select(Connection).where(
                Connection.id == connection_id, Connection.workspace_id == workspace_id
            )
        )
        if connection is None:
            raise _bad_request("connection_id does not reference a connection in this workspace")
    if target_agent_id is not None:
        agent = await db.scalar(
            select(Agent).where(Agent.id == target_agent_id, Agent.workspace_id == workspace_id)
        )
        if agent is None:
            raise _bad_request("target_agent_id does not reference an agent in this workspace")
    if target_team_id is not None:
        team = await db.scalar(
            select(Team).where(Team.id == target_team_id, Team.workspace_id == workspace_id)
        )
        if team is None:
            raise _bad_request("target_team_id does not reference a team in this workspace")


def _validate_filter_document(document: dict[str, Any]) -> None:
    try:
        validate_filter(document)
    except FilterError as exc:
        raise _bad_request(f"Invalid filter: {exc}") from exc


_KNOWN_TEMPLATES = ("engineering_ticket",)


async def _validate_workflow_definition(
    db: AsyncSession, workspace_id: UUID, definition: dict[str, Any] | None
) -> None:
    """Template config validation (plan 8.4). Unknown templates are rejected;
    configured agent ids must reference agents in this workspace."""
    if not definition:
        return
    template = definition.get("template")
    if template not in _KNOWN_TEMPLATES:
        raise _bad_request(
            f"unknown workflow template {template!r}; available: {', '.join(_KNOWN_TEMPLATES)}"
        )
    for key in ("implementer_agent_id", "qa_agent_id"):
        raw = definition.get(key)
        if not raw:
            continue
        try:
            agent_id = UUID(str(raw))
        except ValueError as exc:
            raise _bad_request(f"{key} is not a valid agent id") from exc
        agent = await db.scalar(
            select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        )
        if agent is None:
            raise _bad_request(f"{key} does not reference an agent in this workspace")
    cycles = definition.get("max_retest_cycles")
    if cycles is not None and (not isinstance(cycles, int) or not 1 <= cycles <= 10):
        raise _bad_request("max_retest_cycles must be an integer between 1 and 10")


async def list_triggers(db: AsyncSession, workspace_id: UUID) -> list[Trigger]:
    rows = await db.scalars(
        select(Trigger).where(Trigger.workspace_id == workspace_id).order_by(Trigger.created_at)
    )
    return list(rows)


async def last_invocations(
    db: AsyncSession, workspace_id: UUID, trigger_ids: list[UUID]
) -> dict[UUID, TriggerInvocation]:
    """Most recent invocation per trigger, for the list view (plan 17.10)."""
    if not trigger_ids:
        return {}
    # UUIDv7 ids are time-ordered (and compare bytewise the same way in
    # Postgres and SQLite), so `id desc` picks the newest row — and unlike
    # created_at it can never tie within a transaction. Postgres has no
    # max(uuid) aggregate, hence the window function.
    ranked = (
        select(
            TriggerInvocation.id.label("invocation_id"),
            func.row_number()
            .over(
                partition_by=TriggerInvocation.trigger_id,
                order_by=TriggerInvocation.id.desc(),
            )
            .label("rank"),
        )
        .where(
            TriggerInvocation.workspace_id == workspace_id,
            TriggerInvocation.trigger_id.in_(trigger_ids),
        )
        .subquery()
    )
    rows = await db.scalars(
        select(TriggerInvocation).where(
            TriggerInvocation.id.in_(select(ranked.c.invocation_id).where(ranked.c.rank == 1))
        )
    )
    return {row.trigger_id: row for row in rows}


def _validate_invariants(
    *,
    trigger_type: str,
    event_type: str | None,
    target_agent_id: UUID | None,
    target_team_id: UUID | None,
) -> None:
    """Rules a stored trigger must always satisfy, on create *and* update.

    A connector_event trigger without an event_type matches every connector
    event in the workspace (the matcher only filters when event_type is
    truthy), so clearing it silently widens the trigger far beyond what the
    author configured. A trigger without a target can never dispatch and only
    produces failed invocations.
    """
    if trigger_type == TriggerType.CONNECTOR_EVENT.value and not event_type:
        raise _bad_request("connector_event triggers require an event_type")
    if target_agent_id is None and target_team_id is None:
        raise _bad_request("a trigger needs a target_agent_id or target_team_id to assign work")


async def get_trigger(db: AsyncSession, workspace_id: UUID, trigger_id: UUID) -> Trigger:
    trigger = await db.scalar(
        select(Trigger).where(Trigger.id == trigger_id, Trigger.workspace_id == workspace_id)
    )
    if trigger is None:
        raise _not_found()
    return trigger


async def create_trigger(
    db: AsyncSession,
    ctx: WorkspaceContext,
    payload: TriggerCreate,
    *,
    request_id: UUID,
    ip_hash: str,
) -> Trigger:
    _validate_invariants(
        trigger_type=payload.trigger_type.value,
        event_type=payload.event_type,
        target_agent_id=payload.target_agent_id,
        target_team_id=payload.target_team_id,
    )
    _validate_filter_document(payload.filter)
    await _validate_references(
        db,
        ctx.workspace_id,
        connection_id=payload.connection_id,
        target_agent_id=payload.target_agent_id,
        target_team_id=payload.target_team_id,
    )
    await _validate_workflow_definition(db, ctx.workspace_id, payload.workflow_definition)

    trigger = Trigger(
        workspace_id=ctx.workspace_id,
        name=payload.name,
        enabled=payload.enabled,
        trigger_type=payload.trigger_type.value,
        connection_id=payload.connection_id,
        event_type=payload.event_type,
        filter_json=payload.filter,
        action_type=payload.action_type.value,
        target_agent_id=payload.target_agent_id,
        target_team_id=payload.target_team_id,
        action_config_json=payload.action_config,
        dedupe_window_seconds=payload.dedupe_window_seconds,
        workflow_definition=payload.workflow_definition,
        created_by_user_id=ctx.user.id,
    )
    db.add(trigger)
    await db.flush()
    audit.record(
        db,
        action="trigger.created",
        target_type="trigger",
        target_id=trigger.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": trigger.name, "event_type": trigger.event_type},
    )
    await db.commit()
    return trigger


async def update_trigger(
    db: AsyncSession,
    ctx: WorkspaceContext,
    trigger_id: UUID,
    payload: TriggerUpdate,
    *,
    request_id: UUID,
    ip_hash: str,
) -> Trigger:
    trigger = await get_trigger(db, ctx.workspace_id, trigger_id)
    changes = payload.model_dump(exclude_unset=True)
    if "filter" in changes:
        _validate_filter_document(changes["filter"])
        trigger.filter_json = changes.pop("filter")
    if "action_config" in changes:
        trigger.action_config_json = changes.pop("action_config")
    if "workflow_definition" in changes:
        await _validate_workflow_definition(db, ctx.workspace_id, changes["workflow_definition"])
    await _validate_references(
        db,
        ctx.workspace_id,
        connection_id=changes.get("connection_id"),
        target_agent_id=changes.get("target_agent_id"),
        target_team_id=changes.get("target_team_id"),
    )
    # Same invariants the create path enforces, against the values this
    # update would leave behind — an update may not put a trigger into a
    # shape create would refuse (an unscoped event_type, or no target).
    _validate_invariants(
        trigger_type=trigger.trigger_type,
        event_type=changes.get("event_type", trigger.event_type),
        target_agent_id=changes.get("target_agent_id", trigger.target_agent_id),
        target_team_id=changes.get("target_team_id", trigger.target_team_id),
    )
    for field, value in changes.items():
        setattr(trigger, field, value)
    audit.record(
        db,
        action="trigger.updated",
        target_type="trigger",
        target_id=trigger.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": trigger.name, "fields": sorted(changes.keys())},
    )
    await db.commit()
    return trigger


async def set_enabled(
    db: AsyncSession,
    ctx: WorkspaceContext,
    trigger_id: UUID,
    *,
    enabled: bool,
    request_id: UUID,
    ip_hash: str,
) -> Trigger:
    trigger = await get_trigger(db, ctx.workspace_id, trigger_id)
    trigger.enabled = enabled
    audit.record(
        db,
        action="trigger.enabled" if enabled else "trigger.disabled",
        target_type="trigger",
        target_id=trigger.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": trigger.name},
    )
    await db.commit()
    return trigger


async def delete_trigger(
    db: AsyncSession,
    ctx: WorkspaceContext,
    trigger_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> None:
    trigger = await get_trigger(db, ctx.workspace_id, trigger_id)
    audit.record(
        db,
        action="trigger.deleted",
        target_type="trigger",
        target_id=trigger.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": trigger.name},
    )
    await db.delete(trigger)
    await db.commit()


def test_trigger(trigger: Trigger, event: dict[str, Any]) -> TriggerTestResult:
    """Dry-run evaluation with per-condition explanations (plan 10.3).

    Pure and side-effect free: nothing is recorded, no workflow starts.
    """
    event_type = str(event.get("event_type", ""))
    event_type_matches = not trigger.event_type or trigger.event_type == event_type
    result = evaluate_filter(trigger.filter_json, event)
    return TriggerTestResult(
        matched=event_type_matches and result.matched,
        event_type_matches=event_type_matches,
        filter_matches=result.matched,
        conditions=[ConditionExplanation(**condition.as_dict()) for condition in result.conditions],
    )


async def list_invocations(
    db: AsyncSession, workspace_id: UUID, trigger_id: UUID, *, limit: int = 20
) -> list[TriggerInvocation]:
    rows = await db.scalars(
        select(TriggerInvocation)
        .where(
            TriggerInvocation.workspace_id == workspace_id,
            TriggerInvocation.trigger_id == trigger_id,
        )
        .order_by(TriggerInvocation.id.desc())
        .limit(min(max(limit, 1), 100))
    )
    return list(rows)
