"""Reading and writing one person's onboarding state for one workspace.

The state lives under the ``onboarding`` key of ``workspace_membership
.settings_json`` — server-side, so skipping the tour on a laptop also skips it
on a phone, and a cleared browser cache does not resurrect it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.onboarding.schemas import OnboardingStateOut, OnboardingStatus
from jhin_db.models import WorkspaceMembership

SETTINGS_KEY = "onboarding"

_VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "dismissed", "completed"})


async def _membership(db: AsyncSession, workspace_id: UUID, user_id: UUID) -> WorkspaceMembership:
    membership = await db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )
    if membership is None:
        # The role dependency already proved membership, so this is only
        # reachable if the row vanished mid-request.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return membership


def _parse(raw: Any) -> OnboardingStateOut:
    """Read stored state defensively: anything unrecognised means 'pending'.

    A membership created before this column existed, or one holding a value
    from a future version, must still produce a usable answer — the worst case
    is showing the introduction once more, never a 500 on page load.
    """
    if not isinstance(raw, dict):
        return OnboardingStateOut(status="pending")
    state = raw.get("status")
    last_step = raw.get("last_step")
    updated_at = raw.get("updated_at")
    parsed_updated: datetime | None = None
    if isinstance(updated_at, str):
        try:
            parsed_updated = datetime.fromisoformat(updated_at)
        except ValueError:
            parsed_updated = None
    return OnboardingStateOut(
        status=state if state in _VALID_STATUSES else "pending",
        last_step=last_step if isinstance(last_step, str) else None,
        updated_at=parsed_updated,
    )


async def get_state(db: AsyncSession, workspace_id: UUID, user_id: UUID) -> OnboardingStateOut:
    membership = await _membership(db, workspace_id, user_id)
    return _parse(membership.settings_json.get(SETTINGS_KEY))


async def set_state(
    db: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    new_status: OnboardingStatus,
    last_step: str | None,
) -> OnboardingStateOut:
    membership = await _membership(db, workspace_id, user_id)
    now = datetime.now(UTC)
    stored = {
        "status": new_status,
        "last_step": last_step,
        "updated_at": now.isoformat(),
    }
    # Replaced wholesale rather than mutated: the JSON column is only tracked
    # for changes by identity, so an in-place update would not be persisted.
    membership.settings_json = {**membership.settings_json, SETTINGS_KEY: stored}
    await db.commit()
    return OnboardingStateOut(status=new_status, last_step=last_step, updated_at=now)
