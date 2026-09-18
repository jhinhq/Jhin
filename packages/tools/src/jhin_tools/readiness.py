"""Required inputs and unchanged failure limits, independently enforced at dispatch."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import AgentRun, Message, Task, ToolCall, UserQuestion
from jhin_policy import DecisionType, PolicyDecision, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.scheduling import clock_time, timezone_key

_SETUP_TOOLS = frozenset(
    {
        "organization.ask_person",
        "organization.report_result",
        "organization.respond_work_request",
        "organization.directory.search",
        "organization.colleague_status",
        "memory.search",
        "variables.list",
        "variables.get",
        "variables.set",
        "variables.delete",
        "variables.copy",
        "schedules.list",
        "schedules.history",
    }
)


def failed_observation(row: ToolCall) -> bool:
    if row.status == "failed":
        return True
    output = row.sanitized_output_json or {}
    if row.tool_name.startswith("cli."):
        if output.get("passed") is False or output.get("exit_code") not in (None, 0):
            return True
        # curl can exit zero on an HTTP refusal. Match protocol/status output,
        # not unrelated numbers within an otherwise successful document.
        streams = str(output.get("stdout", "")) + "\n" + str(output.get("stderr", ""))
        return bool(
            re.search(
                r"(?im)^(?:HTTP/[\d.]+\s+|(?:http_status|status_code|http_code)\s*[:=]\s*)(?:401|403|429)\b",
                streams[:40000],
            )
        )
    return False


def validate_answer(value: str, value_type: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Provide the required input")
    if value_type == "url":
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError("Provide an explicit HTTP(S) URL without embedded credentials")
    elif value_type == "timezone":
        value = timezone_key(value)
    elif value_type == "time":
        value = clock_time(value)
    return value


async def record_required_input(
    ctx: ToolExecutionContext, key: str, label: str, value_type: str
) -> None:
    task = await ctx.session.get(Task, ctx.task_id)
    if task is None or task.workspace_id != ctx.workspace_id:
        return
    metadata = dict(task.metadata_json or {})
    blockers = [item for item in metadata.get("required_inputs", []) if isinstance(item, dict)]
    if not any(item.get("key") == key for item in blockers):
        blockers.append({"key": key, "label": label, "value_type": value_type})
        task.metadata_json = {**metadata, "required_inputs": blockers}
        await ctx.session.flush()


async def resolve_required_answer(db: AsyncSession, question: UserQuestion, answer: str) -> str:
    value = validate_answer(answer, question.value_type)
    if question.required and question.input_key and question.task_id:
        task = await db.get(Task, question.task_id)
        if task is not None and task.workspace_id == question.workspace_id:
            metadata = dict(task.metadata_json or {})
            blockers = [
                item
                for item in metadata.get("required_inputs", [])
                if isinstance(item, dict) and item.get("key") != question.input_key
            ]
            resolved = dict(metadata.get("resolved_inputs", {}))
            resolved[question.input_key] = value
            task.metadata_json = {
                **metadata,
                "required_inputs": blockers,
                "resolved_inputs": resolved,
            }
    return value


async def check_work_readiness(
    ctx: ToolExecutionContext, definition: ToolDefinition, payload: BaseModel
) -> PolicyDecision | None:
    """Call after normal authorization; this only narrows existing permissions."""
    if definition.name in _SETUP_TOOLS:
        return None
    if definition.name == "schedules.delete":
        return None
    if definition.name == "schedules.update":
        values = payload.model_dump(exclude_unset=True, exclude_none=True)
        if values.get("enabled") is False and not (
            values.keys() & {"name", "brief", "local_time", "timezone", "weekdays"}
        ):
            # A pending setup question must never prevent disabling automation.
            return None
    task = await ctx.session.get(Task, ctx.task_id)
    if task is None or task.workspace_id != ctx.workspace_id:
        return None
    blockers = [
        item
        for item in (task.metadata_json or {}).get("required_inputs", [])
        if isinstance(item, dict)
    ]
    question = await ctx.session.scalar(
        select(UserQuestion)
        .where(
            UserQuestion.workspace_id == ctx.workspace_id,
            UserQuestion.task_id == ctx.task_id,
            UserQuestion.required.is_(True),
            UserQuestion.status.in_(["pending", "expired"]),
        )
        .order_by(UserQuestion.asked_at)
        .limit(1)
    )
    if blockers or question is not None:
        labels = ", ".join(
            str(item.get("label", item.get("key", "required input")))[:200] for item in blockers[:8]
        )
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="required_input_missing",
            reason=(
                f"Dependent work is blocked pending {labels or 'the required question'}. "
                "Ask the person for the actual input; a delegated agent must return "
                "missing inputs to its requester. Do not guess, probe another URL, "
                "or delegate around this requirement."
            ),
        )
    # The same failed operation may be retried once; a newer user instruction
    # or genuinely different arguments starts a new decision. Completed and
    # uncertain effects retain the gateway's stronger idempotency rules.
    latest_user = await ctx.session.scalar(
        select(Message.created_at)
        .where(
            Message.workspace_id == ctx.workspace_id,
            Message.task_id == ctx.task_id,
            Message.sender_type == "user",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    query = select(ToolCall).where(
        ToolCall.workspace_id == ctx.workspace_id,
        or_(
            ToolCall.run_id == ctx.run_id,
            ToolCall.run_id.in_(
                select(AgentRun.id).where(
                    AgentRun.workspace_id == ctx.workspace_id,
                    AgentRun.task_id == ctx.task_id,
                )
            ),
        ),
        ToolCall.tool_name == definition.name,
        ToolCall.status.in_(["failed", "completed"]),
    )
    if latest_user:
        query = query.where(ToolCall.created_at >= latest_user)
    failures = await ctx.session.scalars(query.order_by(ToolCall.created_at.desc()).limit(30))
    requested = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    identical = sum(
        json.dumps(row.sanitized_input_json, sort_keys=True, separators=(",", ":")) == requested
        for row in failures
        if failed_observation(row)
    )
    if identical >= 2:
        return PolicyDecision(
            decision=DecisionType.DENY,
            code="unchanged_failure_limit",
            reason="This unchanged operation already failed twice. Explain the "
            "failure and obtain new information or fix its cause before "
            "retrying. Respect authentication refusals and Retry-After; do "
            "not repeat completed or uncertain writes.",
        )
    return None
