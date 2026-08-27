"""Trigger CRUD, dry-run evaluation, and invocation reads (plan 10.3, 17.10).

Filters are validated with the same pure DSL the event worker evaluates, so
anything accepted here is exactly what will run. All writes are audited.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from jhin_domain import ActorType, AgentStatus, TriggerInvocationStatus, TriggerType
from jhin_observability import SafeErrorCode
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


# --- Target health (a trigger is only as alive as the agent it assigns to) ---

TARGET_OK = "ok"
TARGET_AGENT_DELETED = "agent_deleted"
TARGET_AGENT_PAUSED = "agent_paused"
TARGET_TEAM_UNSTAFFED = "team_unstaffed"

_TARGET_WARNINGS = {
    TARGET_AGENT_DELETED: (
        "The agent this automation gave work to was deleted, so the automation was switched "
        "off. Edit it to choose another agent, then switch it back on."
    ),
    TARGET_AGENT_PAUSED: (
        "The agent this automation gives work to is paused, so nothing will run. Resume that "
        "agent, or edit the automation to choose another one."
    ),
    TARGET_TEAM_UNSTAFFED: (
        "The team this automation hands work to has no active agent to take it. Add one to "
        "the team, or edit the automation to choose an agent directly."
    ),
}


@dataclass(frozen=True)
class TargetHealth:
    """Whether this trigger still has somewhere to send work, and what to do."""

    state: str
    warning: str | None


def _health(state: str) -> TargetHealth:
    return TargetHealth(state=state, warning=_TARGET_WARNINGS.get(state))


async def _team_has_active_agent(db: AsyncSession, workspace_id: UUID, team_id: UUID) -> bool:
    """The same availability the event worker requires: a manager or member
    that is active. A team of paused agents can take no work."""
    team = await db.scalar(
        select(Team).where(Team.id == team_id, Team.workspace_id == workspace_id)
    )
    candidates = select(Agent).where(
        Agent.workspace_id == workspace_id,
        Agent.status == AgentStatus.ACTIVE.value,
    )
    if team is not None and team.manager_agent_id is not None:
        manager = await db.scalar(candidates.where(Agent.id == team.manager_agent_id))
        if manager is not None:
            return True
    member = await db.scalar(candidates.where(Agent.team_id == team_id))
    return member is not None


async def target_health(db: AsyncSession, workspace_id: UUID, trigger: Trigger) -> TargetHealth:
    """Why this trigger cannot dispatch, in words an admin can act on.

    Create and update both refuse a trigger with no target, so a trigger that
    has neither can only have lost one: ``target_agent_id`` is ``SET NULL``
    when the agent row goes away."""
    if trigger.target_agent_id is not None:
        agent = await db.scalar(
            select(Agent).where(
                Agent.id == trigger.target_agent_id, Agent.workspace_id == workspace_id
            )
        )
        if agent is None:
            return _health(TARGET_AGENT_DELETED)
        return _health(
            TARGET_OK if agent.status == AgentStatus.ACTIVE.value else TARGET_AGENT_PAUSED
        )
    if trigger.target_team_id is not None:
        if await _team_has_active_agent(db, workspace_id, trigger.target_team_id):
            return _health(TARGET_OK)
        return _health(TARGET_TEAM_UNSTAFFED)
    return _health(TARGET_AGENT_DELETED)


async def reconcile_targets(
    db: AsyncSession, workspace_id: UUID, triggers: list[Trigger]
) -> dict[UUID, TargetHealth]:
    """Report each trigger's target health, switching off any whose target is gone.

    Deleting an agent detaches it from every trigger that assigned work to it
    (FK ``SET NULL``) and leaves those triggers enabled, so each matching
    event afterwards produces nothing but a failed invocation. There is no
    target left to restore and no safe guess to make, so such a trigger is
    switched off here — durably, with an audit row saying why — and its
    warning tells the admin how to bring it back."""
    health: dict[UUID, TargetHealth] = {}
    switched_off = False
    for trigger in triggers:
        state = await target_health(db, workspace_id, trigger)
        health[trigger.id] = state
        if state.state != TARGET_AGENT_DELETED or not trigger.enabled:
            continue
        trigger.enabled = False
        switched_off = True
        audit.record(
            db,
            action="trigger.disabled",
            target_type="trigger",
            workspace_id=workspace_id,
            actor_type=ActorType.SYSTEM,
            target_id=trigger.id,
            metadata={"name": trigger.name, "reason": "target agent no longer exists"},
        )
    if switched_off:
        await db.commit()
    return health


# --- Invocation outcomes in words (the stored codes stay internal) ---

_FAILURE_FALLBACK = (
    "This run could not be started. Check the app this automation watches and the agent it "
    "gives work to, then try again."
)
_FAILURE_MESSAGES = {
    SafeErrorCode.INVALID_REQUEST.value: (
        "This run had nowhere to go: no active agent was available to take the work. Check the "
        "agent this automation gives work to."
    ),
    SafeErrorCode.UPSTREAM_UNAVAILABLE.value: (
        "This run could not be started because the service that runs agent work was "
        "unreachable. It is retried automatically."
    ),
}


def invocation_message(
    invocation: TriggerInvocation, health: TargetHealth | None = None
) -> str | None:
    """Plain language for a failed invocation, or None when it did not fail.

    The event worker deliberately persists only closed, payload-free codes, so
    a reader must never be handed one. When the trigger's target explains the
    failure right now — a paused or deleted agent — that explanation wins,
    because it names the thing the admin has to fix."""
    if invocation.status != TriggerInvocationStatus.FAILED.value:
        return None
    error = invocation.error or ""
    if error == SafeErrorCode.INVALID_REQUEST.value and health is not None and health.warning:
        return health.warning
    return _FAILURE_MESSAGES.get(error, _FAILURE_FALLBACK)


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
    if enabled:
        # Switching a targetless trigger back on only restores the failure it
        # was switched off for; the target has to be chosen first.
        health = await target_health(db, ctx.workspace_id, trigger)
        if health.state == TARGET_AGENT_DELETED:
            raise _bad_request(
                "This automation has no agent to give work to — the one it used was deleted. "
                "Edit it to choose another agent, then switch it on."
            )
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
