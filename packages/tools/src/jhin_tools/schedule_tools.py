"""Agent-owned recurring work: current grants still govern every occurrence."""

from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from jhin_db.models.schedule import AgentSchedule, ScheduleOccurrence
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.errors import ToolExecutionError
from jhin_tools.schedule_confirmation import (
    activation_preview,
    confirmation_input_key,
    require_activation_confirmation,
)
from jhin_tools.scheduling import (
    WEEKDAY_NAMES,
    ScheduleCreate,
    ScheduleError,
    ScheduleFields,
    ScheduleOut,
    ScheduleUpdate,
    WeekdayName,
    create_schedule,
    delete_schedule,
    get_schedule,
    update_schedule,
)


def _named_weekdays(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("Select weekday names, such as ['Monday']")
    result = []
    for day in value:
        if isinstance(day, str) and day in WEEKDAY_NAMES:
            result.append(WEEKDAY_NAMES.index(day))
        elif type(day) is int and day in range(7):
            # Additive compatibility for retained calls and text-only clients.
            result.append(day)
        else:
            raise ValueError("Select weekday names, such as ['Monday']")
    return result


class OwnScheduleCreate(ScheduleFields):
    enabled: bool = Field(default=False, description="Agent proposals are always saved paused.")
    weekdays: list[int] = Field(
        default_factory=lambda: list(range(7)),
        min_length=1,
        max_length=7,
        description="Choose weekday names such as ['Monday']; defaults to every day.",
    )
    idempotency_key: str = Field(min_length=1, max_length=150)

    @field_validator("weekdays", mode="before", json_schema_input_type=list[WeekdayName])
    @classmethod
    def named_weekdays(cls, value: Any) -> Any:
        return _named_weekdays(value)


class ScheduleId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: UUID


class OwnScheduleUpdate(ScheduleUpdate):
    schedule_id: UUID
    weekdays: list[int] | None = Field(
        default=None,
        description="Choose weekday names such as ['Monday'].",
    )
    authorized_by_question_id: UUID | None = Field(
        default=None, description="Authenticated human activation answer for this exact revision."
    )

    @field_validator("weekdays", mode="before", json_schema_input_type=list[WeekdayName] | None)
    @classmethod
    def named_weekdays(cls, value: Any) -> Any:
        return _named_weekdays(value)


class OwnScheduleDelete(ScheduleId):
    expected_version: int = Field(ge=1)


class ListSchedules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)


class ScheduleResult(BaseModel):
    schedule: dict[str, Any] | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    detail: str = ""
    confirmation_input_key: str = ""
    activation_preview: dict[str, Any] | None = None


async def _owned(ctx: ToolExecutionContext, schedule_id: UUID) -> AgentSchedule:
    row = await get_schedule(ctx.session, ctx.workspace_id, schedule_id, lock=True)
    if row.agent_id != ctx.agent_id:
        raise ScheduleError("This schedule belongs to another agent", 403)
    return row


def _receipt(row: AgentSchedule, *, detail: str) -> ScheduleResult:
    return ScheduleResult(
        schedule=ScheduleOut.model_validate(row).model_dump(mode="json"),
        confirmation_input_key=confirmation_input_key(row) if not row.enabled else "",
        activation_preview=activation_preview(row) if not row.enabled else None,
        detail=detail,
    )


async def _create(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(OwnScheduleCreate, payload)
    row = await create_schedule(
        ctx.session,
        ctx.workspace_id,
        ScheduleCreate(
            **{
                **data.model_dump(),
                "enabled": False,
                "agent_id": ctx.agent_id,
                "idempotency_key": f"agent:{ctx.agent_id}:{data.idempotency_key}",
            }
        ),
        agent_id=ctx.agent_id,
    )
    return _receipt(
        row,
        detail=(
            "This existing schedule is already active; creation was not repeated."
            if row.enabled
            else "Proposal saved paused; no work will run. Verify that the standing brief, "
            "destination, publishing authority and required setup are complete. Then call "
            "organization.ask_person with confirmation_input_key to show the exact proposal. "
            "Only the authenticated activation option can enable it using schedules.update "
            "authorized_by_question_id. Do not ask to activate an incomplete proposal."
        ),
    )


async def _list(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ListSchedules, payload)
    rows = await ctx.session.scalars(
        select(AgentSchedule)
        .where(
            AgentSchedule.workspace_id == ctx.workspace_id,
            AgentSchedule.agent_id == ctx.agent_id,
            AgentSchedule.deleted_at.is_(None),
        )
        .order_by(AgentSchedule.created_at.desc())
        .limit(data.limit)
        .offset(data.offset)
    )
    return ScheduleResult(
        items=[
            {
                **ScheduleOut.model_validate(row).model_dump(mode="json"),
                "confirmation_input_key": confirmation_input_key(row) if not row.enabled else "",
                "activation_preview": activation_preview(row) if not row.enabled else None,
            }
            for row in rows
        ]
    )


async def _update(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(OwnScheduleUpdate, payload)
    original = await _owned(ctx, data.schedule_id)
    if original.version != data.expected_version or original.deleted_at is not None:
        raise ScheduleError("Schedule changed; reload before editing", 409)
    changes = data.model_dump(
        exclude={"schedule_id", "authorized_by_question_id"}, exclude_unset=True, exclude_none=True
    )
    checked = ScheduleFields.model_validate(
        {
            **{key: getattr(original, key) for key in ScheduleFields.model_fields},
            **{key: value for key, value in changes.items() if key != "expected_version"},
        }
    )
    edits_work = any(
        getattr(checked, key) != getattr(original, key)
        for key in ("name", "brief", "local_time", "timezone", "weekdays")
    )
    if edits_work:
        # A new brief or cadence is a new proposal, including edits of active work.
        changes["enabled"] = False
    elif data.enabled is True:
        if original.enabled:
            return _receipt(
                original, detail="Schedule is already active; activation was not repeated."
            )
        if data.authorized_by_question_id is None:
            return _receipt(
                original,
                detail="Schedule remains paused pending human confirmation. "
                "Use organization.ask_person with confirmation_input_key, then pass "
                "its accepted question_id as authorized_by_question_id to activate.",
            )
        await require_activation_confirmation(ctx, original, data.authorized_by_question_id)
    row = await update_schedule(
        ctx.session,
        ctx.workspace_id,
        data.schedule_id,
        ScheduleUpdate(**changes),
        agent_id=ctx.agent_id,
    )
    return _receipt(
        row,
        detail=(
            "Schedule activated under its current permissions."
            if row.enabled
            else "Schedule saved paused; no new occurrence will start. Activation requires "
            "the authenticated answer to its exact confirmation_input_key. Any "
            "already-started work retains its original brief."
        ),
    )


async def _delete(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(OwnScheduleDelete, payload)
    await _owned(ctx, data.schedule_id)
    await delete_schedule(
        ctx.session,
        ctx.workspace_id,
        data.schedule_id,
        data.expected_version,
        agent_id=ctx.agent_id,
    )
    return ScheduleResult(detail="Schedule retired; execution history is retained.")


async def _history(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(ScheduleId, payload)
    await _owned(ctx, data.schedule_id)
    rows = await ctx.session.scalars(
        select(ScheduleOccurrence)
        .where(
            ScheduleOccurrence.workspace_id == ctx.workspace_id,
            ScheduleOccurrence.schedule_id == data.schedule_id,
        )
        .order_by(ScheduleOccurrence.scheduled_for.desc())
        .limit(50)
    )
    return ScheduleResult(
        items=[
            {
                "id": str(row.id),
                "task_id": str(row.task_id) if row.task_id else None,
                "scheduled_for": row.scheduled_for.isoformat(),
                "status": row.status,
                "error_code": row.error_code,
            }
            for row in rows
        ]
    )


def _guard(executor: ToolExecutor) -> ToolExecutor:
    async def run(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
        try:
            return await executor(ctx, payload)
        except ScheduleError as exc:
            raise ToolExecutionError(
                str(exc), code="schedule_invalid", hint=str(exc), side_effect_possible=False
            ) from exc
        except ValidationError as exc:
            raise ToolExecutionError(
                "Invalid schedule",
                code="schedule_invalid",
                hint="Provide local time HH:MM, an IANA timezone, valid weekdays "
                "and a brief without credentials.",
                side_effect_possible=False,
            ) from exc

    return run


SCHEDULE_TOOLS = tuple(
    (
        ToolDefinition(
            name=f"schedules.{name}",
            description=description,
            risk=RiskLevel.READ if name in {"list", "history"} else RiskLevel.WRITE,
            input_model=model,
            output_model=ScheduleResult,
            required_capability="schedules.read"
            if name in {"list", "history"}
            else "schedules.manage",
            supports_approval=name not in {"list", "history"},
        ),
        _guard(executor),
        None,
    )
    for name, description, model, executor in (
        (
            "create",
            "Prepare a paused recurring-work proposal only when the person requests automation. "
            "A statement of posting days or times is a preference, not a request to create work. "
            "First establish the complete work, destination and publishing authority, "
            "standing brief, exact local HH:MM time and explicit IANA "
            "timezone. List existing schedules first to avoid duplicates. Use "
            "a stable idempotency_key. Publishing restrictions remain in "
            "force. Select weekday names such as ['Monday']. Creation never activates work; "
            "ask_person with the returned confirmation_input_key reviews the exact proposal.",
            OwnScheduleCreate,
            _create,
        ),
        (
            "list",
            "List your recurring schedules with their next run and version.",
            ListSchedules,
            _list,
        ),
        (
            "update",
            "Change your schedule using its expected_version. enabled=false "
            "pauses immediately; true requires authorized_by_question_id from an owner/admin "
            "selecting Activate on the exact platform confirmation. Changes to work or cadence "
            "save paused for renewed confirmation. Use weekday names such as ['Monday']. "
            "Already-started tasks are not cancelled.",
            OwnScheduleUpdate,
            _update,
        ),
        (
            "delete",
            "Retire your recurring schedule, preserving execution history. "
            "Requires current expected_version.",
            OwnScheduleDelete,
            _delete,
        ),
        (
            "history",
            "Inspect the last 50 occurrences of your recurring schedule and their task links.",
            ScheduleId,
            _history,
        ),
    )
)
