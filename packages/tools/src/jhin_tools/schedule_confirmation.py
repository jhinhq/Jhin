"""Human activation of an exact paused schedule, independent of model assertions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Message, Task, UserQuestion
from jhin_db.models.schedule import AgentSchedule
from jhin_secrets.authority import human_content_authorized
from jhin_tools.errors import ToolExecutionError
from jhin_tools.scheduling import ScheduleFields, ScheduleOut, get_schedule, next_occurrence

if TYPE_CHECKING:
    from jhin_tools.ask_person import AskPersonInput
    from jhin_tools.builtin import ToolExecutionContext


PREFIX = "schedule_activate_"
_QUESTION = "Activate this recurring work?"
_OPTIONS = [
    {
        "label": "Activate recurring work",
        "value": "activate",
        "detail": "The standing brief and required setup are complete; run this schedule.",
    },
    {
        "label": "Keep paused / setup incomplete",
        "value": "keep_paused",
        "detail": "Save the proposal without running it.",
    },
]


def confirmation_input_key(row: AgentSchedule) -> str:
    fields = {key: getattr(row, key) for key in ScheduleFields.model_fields}
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:24]
    return f"{PREFIX}{row.id.hex}_{row.version}_{digest}"


def activation_preview(row: AgentSchedule, now: datetime | None = None) -> dict[str, Any]:
    preview = ScheduleOut.model_validate(row).model_copy(
        update={
            "enabled": True,
            "next_run_at": next_occurrence(
                now or datetime.now(UTC), row.local_time, row.timezone, row.weekdays
            ),
        }
    )
    return preview.model_dump(mode="json")


def confirmation_context(row: AgentSchedule, now: datetime) -> str:
    preview = activation_preview(row, now)
    return (
        f"Schedule: {row.name}\n"
        f"When: {', '.join(preview['weekday_names'])} at {row.local_time} ({row.timezone})\n"
        f"Next eligible run: {preview['next_run_display']}\n\n"
        f"Standing brief:\n{row.brief}\n\n"
        "Approve only if the destination, work to perform, and any publishing authority "
        "are correct and required setup is complete. Choose Keep paused if information "
        "is missing. Existing connector permissions and editorial approval still apply "
        "to every occurrence. This approves this schedule revision only."
    )


def confirmation_required() -> ToolExecutionError:
    return ToolExecutionError(
        "This schedule needs an authenticated owner's or admin's confirmation.",
        code="schedule_confirmation_required",
        hint="Keep the schedule paused. Ask organization.ask_person with the returned "
        "confirmation_input_key; its platform-owned card shows the exact proposed work. "
        "Only its Activate recurring work option authorizes schedules.update enabled=true "
        "with authorized_by_question_id. A posting preference or free-text yes is not activation.",
        side_effect_possible=False,
    )


async def canonicalize_schedule_question(
    ctx: ToolExecutionContext, data: AskPersonInput, *, now: datetime
) -> None:
    if not data.input_key.startswith(PREFIX):
        return
    from jhin_tools.ask_person import AskPersonOption

    suffix = data.input_key.removeprefix(PREFIX)
    try:
        schedule_id = UUID(hex=suffix.split("_", 1)[0])
        row = await get_schedule(ctx.session, ctx.workspace_id, schedule_id, lock=True)
    except ValueError as exc:
        raise confirmation_required() from exc
    if (
        row.agent_id != ctx.agent_id
        or row.enabled
        or row.deleted_at is not None
        or data.input_key != confirmation_input_key(row)
    ):
        raise confirmation_required()
    # This is trusted platform text from the persisted proposal. It may be
    # longer than the model's bounded context field because review must not hide
    # or truncate any part of the standing brief the human is approving.
    data.question = _QUESTION
    data.context = confirmation_context(row, now)
    data.options = [AskPersonOption(**option) for option in _OPTIONS]
    data.kind = "open"
    data.value_type = "text"
    data.required = False
    data.allow_other = True


async def require_activation_confirmation(
    ctx: ToolExecutionContext, row: AgentSchedule, question_id: UUID | None
) -> None:
    if question_id is None:
        raise confirmation_required()
    question = await ctx.session.get(UserQuestion, question_id)
    task = await ctx.session.get(Task, ctx.task_id)
    if (
        question is None
        or task is None
        or task.workspace_id != ctx.workspace_id
        or task.conversation_id is None
        or question.workspace_id != ctx.workspace_id
        or question.conversation_id != task.conversation_id
        or question.agent_id != ctx.agent_id
        or question.input_key != confirmation_input_key(row)
        or question.status != "answered"
        or question.answer_kind != "option"
        or question.answer_option_value != "activate"
        or question.answered_by_user_id is None
        or question.answered_at is None
        or question.granted_authority != "workspace"
        or question.question != _QUESTION
        or question.options_json != _OPTIONS
        or question.context != confirmation_context(row, question.asked_at)
    ):
        raise confirmation_required()
    source_task = await ctx.session.get(Task, question.task_id) if question.task_id else None
    if (
        source_task is None
        or source_task.workspace_id != ctx.workspace_id
        or source_task.conversation_id != question.conversation_id
    ):
        raise confirmation_required()
    authority = (source_task.metadata_json or {}).get("resolved_input_authority", {})
    proof = authority.get(question.input_key, {}) if isinstance(authority, dict) else {}
    if not isinstance(proof, dict) or not await human_content_authorized(
        ctx.session,
        ctx.workspace_id,
        question.answered_by_user_id,
        proof,
        required_scope="automations:write",
    ):
        raise confirmation_required()


async def retire_confirmation_questions(db: AsyncSession, row: AgentSchedule) -> None:
    """Version changes retire stale review cards and any legacy required blockers."""
    prefix = f"{PREFIX}{row.id.hex}_"
    questions = await db.scalars(
        select(UserQuestion)
        .where(
            UserQuestion.workspace_id == row.workspace_id,
            UserQuestion.input_key.startswith(prefix, autoescape=True),
            UserQuestion.status.in_(["pending", "expired"]),
        )
        .with_for_update()
    )
    for question in questions:
        question.status = "cancelled"
        if question.task_id:
            task = await db.get(Task, question.task_id)
            if task is not None and task.workspace_id == row.workspace_id:
                metadata = task.metadata_json or {}
                task.metadata_json = {
                    **metadata,
                    "required_inputs": [
                        item
                        for item in metadata.get("required_inputs", [])
                        if isinstance(item, dict) and item.get("key") != question.input_key
                    ],
                }
        if question.message_id:
            message = await db.get(Message, question.message_id)
            if message is not None and message.workspace_id == row.workspace_id:
                message.content_json = {
                    **message.content_json,
                    "status": "cancelled",
                    "text": "This activation request was cancelled because the schedule changed.",
                }
