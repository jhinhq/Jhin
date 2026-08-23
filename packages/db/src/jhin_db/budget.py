"""Monthly model-spend budgets (plan 15.5).

Two budgets bound one calendar month (UTC) of tracked spend, the sum of
``agent_run.estimated_cost_micros``:

- the per-agent budget, stored in cents on ``agent.monthly_budget_cents``;
- the workspace budget, stored in micro-dollars under
  ``workspace.settings_json["budget"]["monthly_budget_micros"]``.

All comparisons happen in micro-dollars. The helpers live here — next to the
models — because both the API (attention/spend views) and the agent worker
(run admission and the per-step check) need the same arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import AgentRun

MICROS_PER_CENT = 10_000
DEFAULT_WARNING_THRESHOLD = 0.8


def month_start_utc(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month."""
    current = now or datetime.now(UTC)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def workspace_budget_settings(settings_json: dict[str, Any] | None) -> tuple[int | None, float]:
    """``(monthly_budget_micros, warning_threshold)`` from ``settings_json.budget``.

    Missing or malformed settings mean "no budget" with the default warning
    threshold — budgets fail open on shape, never on arithmetic.
    """
    raw = (settings_json or {}).get("budget")
    if not isinstance(raw, dict):
        return None, DEFAULT_WARNING_THRESHOLD
    budget = raw.get("monthly_budget_micros")
    threshold = raw.get("warning_threshold", DEFAULT_WARNING_THRESHOLD)
    budget_micros = int(budget) if isinstance(budget, int | float) and budget >= 0 else None
    warning = float(threshold) if isinstance(threshold, int | float) else DEFAULT_WARNING_THRESHOLD
    return budget_micros, min(max(warning, 0.0), 1.0)


async def month_spend_micros(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    agent_id: UUID | None = None,
    now: datetime | None = None,
) -> int:
    """Tracked spend since the start of the current UTC month, in micro-dollars."""
    stmt = (
        select(func.coalesce(func.sum(AgentRun.estimated_cost_micros), 0))
        .select_from(AgentRun)
        .where(
            AgentRun.workspace_id == workspace_id,
            AgentRun.created_at >= month_start_utc(now),
        )
    )
    if agent_id is not None:
        stmt = stmt.where(AgentRun.agent_id == agent_id)
    return int(await session.scalar(stmt) or 0)


def format_budget_dollars(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}"


def agent_budget_message(agent_name: str, budget_micros: int) -> str:
    return (
        f"{agent_name} reached its monthly budget ({format_budget_dollars(budget_micros)}) — "
        "raise it in the agent's settings or wait for next month."
    )


def workspace_budget_message(budget_micros: int) -> str:
    return (
        f"This workspace reached its monthly model budget "
        f"({format_budget_dollars(budget_micros)}) — raise it in workspace Settings "
        "or wait for next month."
    )


async def budget_denial_message(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_id: UUID,
    agent_name: str,
    agent_budget_cents: int | None,
    workspace_settings_json: dict[str, Any] | None,
    now: datetime | None = None,
) -> str | None:
    """The friendly stop message when a monthly budget is already met.

    The more specific agent budget wins the naming when both are exhausted.
    ``None`` means both budgets (where set) still have headroom.
    """
    if agent_budget_cents is not None:
        agent_budget_micros = agent_budget_cents * MICROS_PER_CENT
        spent = await month_spend_micros(session, workspace_id, agent_id=agent_id, now=now)
        if spent >= agent_budget_micros:
            return agent_budget_message(agent_name, agent_budget_micros)
    workspace_budget_micros, _threshold = workspace_budget_settings(workspace_settings_json)
    if workspace_budget_micros is not None:
        spent = await month_spend_micros(session, workspace_id, now=now)
        if spent >= workspace_budget_micros:
            return workspace_budget_message(workspace_budget_micros)
    return None
