"""Making the two-step claim reversible.

``tool_call.status`` gained a value in this change: ``claimed``, the durable
half-step that proves an executor was never entered. The column is a plain
``varchar``, so *deploying* it needs no migration and *rolling back* needs no
migration either — but a row left sitting in ``claimed`` is a value the
previous release has never heard of, and the previous release does not shrug
at those. It raises ``GatewayStateError("tool call ... has unexpected status
'claimed'")`` from the same three doorways that would otherwise recover the
call, and the activity turns that into a non-retryable failure. One row, one
dead run, and an exception nobody rolling back at speed will enjoy reading.

So the rollback has a step, and this module is it: close every ``claimed``
row before the old image starts, as a *failure*.

Why a failure rather than ``executing``, which is what the old release wrote
at this moment in the call's life. Because ``claimed`` means something exact,
and the fact does not stop being true because the code that knew how to read
it has been removed: the dispatch compare-and-set commits before the executor
is entered, so a row still in ``claimed`` is proof that nothing ran. Writing
``executing`` would throw that proof away and hand the operator a pile of
"manual reconciliation is required" for calls that provably did nothing. The
old gateway replays a terminal row as its outcome, so a ``failed`` row with
an explanation reaches the agent as an ordinary failed tool call — which is
the true story, and one it already knows how to read.

Racing is safe in both directions. The update is guarded on ``claimed``, so a
row a live worker dispatches first is left alone; and a live worker whose
compare-and-set loses to this command reads a terminal row and replays its
outcome rather than dispatching. The command is therefore not required to be
run with the workers down — it is merely much less interesting when they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db.models import AuditEvent, ToolCall
from jhin_domain import ActorType, ToolCallStatus

#: What a call closed by the rollback is recorded as. Its own code rather than
#: a borrowed one: an operator reading the table afterwards should be able to
#: tell a deploy from a tool that failed.
ROLLED_BACK_CODE = "rolled_back_before_dispatch"

_REASON = (
    "this tool call was claimed but never dispatched, and the release that "
    "understood that state was rolled back; nothing ran and nothing outside "
    "Jhin can have changed"
)
_HINT = (
    "This call never started. Nothing was done and nothing was changed. Call "
    "the tool again if the work still needs doing."
)


@dataclass(frozen=True)
class RollbackReport:
    """Which calls were closed, or would be."""

    closed: tuple[UUID, ...]
    dry_run: bool

    def describe(self) -> str:
        if not self.closed:
            return "no tool_call rows are in 'claimed'; nothing to close"
        verb = "would close" if self.dry_run else "closed"
        lines = [f"{verb} {len(self.closed)} tool_call row(s) left in 'claimed'"]
        lines.extend(f"  {call_id}" for call_id in self.closed)
        return "\n".join(lines)


async def close_claimed_tool_calls(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RollbackReport:
    """Close every ``claimed`` tool call as a failure the old release can read.

    Idempotent: a second run finds nothing, because the first left nothing in
    ``claimed``. Safe to run before the rollback, after it, or twice.
    """
    moment = now or datetime.now(UTC)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ToolCall.id, ToolCall.workspace_id, ToolCall.tool_name).where(
                    ToolCall.status == ToolCallStatus.CLAIMED.value
                )
            )
        ).all()
        if dry_run or not rows:
            return RollbackReport(closed=tuple(row.id for row in rows), dry_run=dry_run)

        closed: list[UUID] = []
        for call_id, workspace_id, tool_name in rows:
            updated = await session.scalar(
                sa_update(ToolCall)
                .where(
                    ToolCall.id == call_id,
                    # Guarded, so a worker that dispatched this row between
                    # the select and here keeps its own outcome.
                    ToolCall.status == ToolCallStatus.CLAIMED.value,
                )
                .values(
                    status=ToolCallStatus.FAILED.value,
                    completed_at=moment,
                    error_code=ROLLED_BACK_CODE,
                    sanitized_output_json={
                        "error": ROLLED_BACK_CODE,
                        "hint": _HINT,
                        "detail": _REASON,
                    },
                )
                .returning(ToolCall.id)
                .execution_options(synchronize_session=False)
            )
            if updated is None:
                continue
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action="tool.call.failed",
                    target_type="tool_call",
                    target_id=call_id,
                    metadata_json={
                        "code": ROLLED_BACK_CODE,
                        "tool_name": tool_name,
                        "evidence": (
                            "the tool_call row was still 'claimed', and only the dispatch "
                            "compare-and-set leaves that state, so no executor ran"
                        ),
                    },
                )
            )
            closed.append(call_id)
        await session.commit()
    return RollbackReport(closed=tuple(closed), dry_run=False)


__all__ = ["ROLLED_BACK_CODE", "RollbackReport", "close_claimed_tool_calls"]
