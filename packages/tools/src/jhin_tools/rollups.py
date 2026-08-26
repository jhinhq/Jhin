"""Status digests derived from authoritative rows.

Two projections live here, both built only from authoritative rows (tasks,
runs, approvals, reviews, work requests, reported results) — never from
transcripts, private memory, or conversation text:

* :func:`build_manager_rollup` — a manager's view of their direct and
  indirect reports. Every item carries the ids it was derived from so a
  reader can open the source. It serves the API (``GET
  /agents/{id}/rollup``) and the manager's prompt context
  (:func:`render_manager_rollup`).
* :func:`build_colleague_status` — *anyone's* view of one colleague, behind
  ``organization.colleague_status``. Strictly narrower than the rollup: see
  :class:`ColleagueStatus` for the allowlist and the reasoning behind it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Agent, AgentRun, Approval, Task, WorkRequest, WorkReview
from jhin_domain import (
    RUN_ACTIVE_STATUSES,
    WORK_REQUEST_OPEN_STATUSES,
    AgentStatus,
    ApprovalStatus,
    RunStatus,
    TaskState,
    WorkReviewStatus,
)

ROLLUP_RECENT_WINDOW = timedelta(days=7)
ROLLUP_MAX_REPORTS = 100
ROLLUP_MAX_ITEMS = 20
ROLLUP_MAX_DEPTH = 10
ROLLUP_MAX_CHARS = 4_000


class ReportSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    name: str
    role_title: str = ""
    depth: int = 1  # 1 = direct report
    status: str = "active"
    availability: str = "available"
    active_tasks: int = 0
    queued_tasks: int = 0
    active_runs: int = 0
    max_concurrent_runs: int = 1


class RollupItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str  # task | run | approval | review | work_request
    source_id: str
    agent_id: str | None = None
    agent_name: str | None = None
    title: str = ""
    status: str = ""
    summary: str = ""
    occurred_at: datetime
    task_id: str | None = None
    conversation_id: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class QueueState(BaseModel):
    model_config = ConfigDict(frozen=True)

    active_runs: int = 0
    queued_tasks: int = 0
    waiting_approval: int = 0
    waiting_delegation: int = 0
    open_work_requests: int = 0


class ManagerRollup(BaseModel):
    model_config = ConfigDict(frozen=True)

    manager_agent_id: str
    generated_at: datetime
    window_start: datetime
    reports: list[ReportSummary] = Field(default_factory=list)
    active_work: list[RollupItem] = Field(default_factory=list)
    recent_work: list[RollupItem] = Field(default_factory=list)
    blocked_or_failed: list[RollupItem] = Field(default_factory=list)
    pending_reviews: list[RollupItem] = Field(default_factory=list)
    pending_approvals: list[RollupItem] = Field(default_factory=list)
    outcomes: list[RollupItem] = Field(default_factory=list)
    open_work_requests: list[RollupItem] = Field(default_factory=list)
    queue: QueueState = Field(default_factory=QueueState)
    source_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


async def report_agents(
    session: AsyncSession, manager: Agent, *, include_indirect: bool = True
) -> list[tuple[Agent, int]]:
    """Direct (depth 1) and indirect reports, breadth-first, bounded."""
    out: list[tuple[Agent, int]] = []
    seen: set[UUID] = {manager.id}
    frontier = [manager.id]
    depth = 0
    while frontier and depth < ROLLUP_MAX_DEPTH and len(out) < ROLLUP_MAX_REPORTS:
        depth += 1
        rows = list(
            await session.scalars(
                select(Agent)
                .where(
                    Agent.workspace_id == manager.workspace_id,
                    Agent.manager_agent_id.in_(frontier),
                )
                .order_by(Agent.name, Agent.id)
            )
        )
        frontier = []
        for agent in rows:
            if agent.id in seen:
                continue
            seen.add(agent.id)
            out.append((agent, depth))
            frontier.append(agent.id)
            if len(out) >= ROLLUP_MAX_REPORTS:
                break
        if not include_indirect:
            break
    return out


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive timestamps; treat them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sorted(items: list[RollupItem]) -> list[RollupItem]:
    return sorted(items, key=lambda i: (i.occurred_at, i.source_id), reverse=True)[
        :ROLLUP_MAX_ITEMS
    ]


async def build_manager_rollup(
    session: AsyncSession,
    manager: Agent,
    *,
    include_indirect: bool = True,
    now: datetime | None = None,
) -> ManagerRollup:
    now = now or datetime.now(UTC)
    window_start = now - ROLLUP_RECENT_WINDOW
    workspace_id = manager.workspace_id
    reports = await report_agents(session, manager, include_indirect=include_indirect)
    agent_ids = [a.id for a, _ in reports]
    names = {a.id: a.name for a, _ in reports}

    if not agent_ids:
        return ManagerRollup(
            manager_agent_id=str(manager.id), generated_at=now, window_start=window_start
        )

    tasks = list(
        await session.scalars(
            select(Task)
            .where(
                Task.workspace_id == workspace_id,
                Task.assigned_agent_id.in_(agent_ids),
                Task.updated_at >= window_start,
            )
            .order_by(Task.updated_at.desc(), Task.id.desc())
            .limit(500)
        )
    )
    all_active_tasks = list(
        await session.scalars(
            select(Task).where(
                Task.workspace_id == workspace_id,
                Task.assigned_agent_id.in_(agent_ids),
                Task.state.in_(
                    (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value)
                ),
            )
        )
    )
    task_by_id = {t.id: t for t in [*tasks, *all_active_tasks]}
    runs = list(
        await session.scalars(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.agent_id.in_(agent_ids),
                AgentRun.status.in_([s.value for s in RUN_ACTIVE_STATUSES]),
            )
        )
    )
    approvals = list(
        await session.scalars(
            select(Approval).where(
                Approval.workspace_id == workspace_id,
                Approval.requested_by_agent_id.in_(agent_ids),
                Approval.status == ApprovalStatus.PENDING.value,
            )
        )
    )
    reviews = list(
        await session.scalars(
            select(WorkReview).where(
                WorkReview.workspace_id == workspace_id,
                WorkReview.status == WorkReviewStatus.PENDING.value,
                (WorkReview.subject_agent_id.in_(agent_ids))
                | (WorkReview.reviewer_agent_id == manager.id),
            )
        )
    )
    requests = list(
        await session.scalars(
            select(WorkRequest).where(
                WorkRequest.workspace_id == workspace_id,
                WorkRequest.status.in_([s.value for s in WORK_REQUEST_OPEN_STATUSES]),
                (WorkRequest.target_agent_id.in_(agent_ids))
                | (WorkRequest.requester_agent_id.in_(agent_ids)),
            )
        )
    )

    def task_item(task: Task, *, status: str | None = None, summary: str = "") -> RollupItem:
        reported = task.metadata_json.get("reported_result")
        reported = reported if isinstance(reported, dict) else {}
        return RollupItem(
            kind="task",
            source_id=str(task.id),
            agent_id=str(task.assigned_agent_id) if task.assigned_agent_id else None,
            agent_name=names.get(task.assigned_agent_id) if task.assigned_agent_id else None,
            title=task.title,
            status=status or task.state,
            summary=summary or str(reported.get("summary", "") or "")[:400],
            occurred_at=_aware(task.updated_at),
            task_id=str(task.id),
            conversation_id=str(task.conversation_id) if task.conversation_id else None,
            artifacts=[a for a in reported.get("artifacts", []) if isinstance(a, dict)][:10],
            risks=[str(r) for r in reported.get("risks", [])][:10],
        )

    run_status_by_task: dict[UUID, str] = {}
    for run in runs:
        if run.task_id is not None:
            run_status_by_task[run.task_id] = run.status

    active_work: list[RollupItem] = []
    recent_work: list[RollupItem] = []
    blocked_or_failed: list[RollupItem] = []
    outcomes: list[RollupItem] = []
    for task in task_by_id.values():
        run_status = run_status_by_task.get(task.id)
        if task.state in (TaskState.QUEUED.value, TaskState.RUNNING.value, TaskState.PAUSED.value):
            label = run_status or task.state
            item = task_item(task, status=label)
            active_work.append(item)
            if run_status in (RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_DELEGATION.value):
                blocked_or_failed.append(item)
        elif _aware(task.updated_at) >= window_start:
            item = task_item(task)
            recent_work.append(item)
            if task.state == TaskState.FAILED.value:
                blocked_or_failed.append(item)
            reported = task.metadata_json.get("reported_result")
            if isinstance(reported, dict) and (
                task.state == TaskState.COMPLETED.value
                or reported.get("status") in ("fail", "blocked")
            ):
                outcomes.append(item)
                if reported.get("status") == "blocked":
                    blocked_or_failed.append(item)

    pending_approvals = [
        RollupItem(
            kind="approval",
            source_id=str(a.id),
            agent_id=str(a.requested_by_agent_id) if a.requested_by_agent_id else None,
            agent_name=names.get(a.requested_by_agent_id) if a.requested_by_agent_id else None,
            title=a.action_type,
            status=a.status,
            summary=a.reason[:400],
            occurred_at=_aware(a.requested_at),
            task_id=str(a.task_id) if a.task_id else None,
            conversation_id=(
                str(task_by_id[a.task_id].conversation_id)
                if a.task_id in task_by_id and task_by_id[a.task_id].conversation_id
                else None
            ),
        )
        for a in approvals
    ]
    pending_reviews = [
        RollupItem(
            kind="review",
            source_id=str(r.id),
            agent_id=str(r.subject_agent_id) if r.subject_agent_id else None,
            agent_name=names.get(r.subject_agent_id) if r.subject_agent_id else None,
            title=f"{r.mode} review",
            status=("assigned_to_you" if r.reviewer_agent_id == manager.id else r.reviewer_type),
            summary=str(r.evidence_json.get("summary", "") or "")[:400],
            occurred_at=_aware(r.requested_at),
            task_id=str(r.task_id) if r.task_id else None,
        )
        for r in reviews
    ]
    open_requests = [
        RollupItem(
            kind="work_request",
            source_id=str(w.id),
            agent_id=str(w.target_agent_id),
            agent_name=names.get(w.target_agent_id)
            or str(w.metadata_json.get("target_agent_name", "") or ""),
            title=w.title,
            status=w.status,
            summary=(
                f"from {w.metadata_json.get('requester_agent_name', 'an agent')}"
                if w.target_agent_id in names
                else f"to {w.metadata_json.get('target_agent_name', 'an agent')}"
            ),
            occurred_at=_aware(w.created_at),
            task_id=str(w.requester_task_id) if w.requester_task_id else None,
            conversation_id=str(w.conversation_id) if w.conversation_id else None,
        )
        for w in requests
    ]

    report_summaries: list[ReportSummary] = []
    for agent, depth in reports:
        agent_tasks = [t for t in task_by_id.values() if t.assigned_agent_id == agent.id]
        report_summaries.append(
            ReportSummary(
                agent_id=str(agent.id),
                name=agent.name,
                role_title=agent.role_title,
                depth=depth,
                status=agent.status,
                availability=agent.availability,
                active_tasks=sum(
                    1
                    for t in agent_tasks
                    if t.state in (TaskState.RUNNING.value, TaskState.PAUSED.value)
                ),
                queued_tasks=sum(1 for t in agent_tasks if t.state == TaskState.QUEUED.value),
                active_runs=sum(1 for r in runs if r.agent_id == agent.id),
                max_concurrent_runs=agent.max_concurrent_runs,
            )
        )

    queue = QueueState(
        active_runs=len(runs),
        queued_tasks=sum(1 for t in task_by_id.values() if t.state == TaskState.QUEUED.value),
        waiting_approval=sum(1 for r in runs if r.status == RunStatus.WAITING_APPROVAL.value),
        waiting_delegation=sum(1 for r in runs if r.status == RunStatus.WAITING_DELEGATION.value),
        open_work_requests=len(open_requests),
    )
    sections = {
        "active_work": _sorted(active_work),
        "recent_work": _sorted(recent_work),
        "blocked_or_failed": _sorted(list({i.source_id: i for i in blocked_or_failed}.values())),
        "pending_reviews": _sorted(pending_reviews),
        "pending_approvals": _sorted(pending_approvals),
        "outcomes": _sorted(outcomes),
        "open_work_requests": _sorted(open_requests),
    }
    truncated = (
        any(
            len(full) > ROLLUP_MAX_ITEMS
            for full in (
                active_work,
                recent_work,
                blocked_or_failed,
                pending_reviews,
                pending_approvals,
                outcomes,
                open_requests,
            )
        )
        or len(reports) >= ROLLUP_MAX_REPORTS
    )
    source_ids = sorted({i.source_id for items in sections.values() for i in items})
    return ManagerRollup(
        manager_agent_id=str(manager.id),
        generated_at=now,
        window_start=window_start,
        reports=report_summaries,
        queue=queue,
        source_ids=source_ids,
        truncated=truncated,
        **sections,
    )


def _item_line(item: RollupItem) -> str:
    who = item.agent_name or "?"
    line = f"- {who}: {item.title[:80]} [{item.status}] (id {item.source_id})"
    if item.summary:
        line += f" — {item.summary[:160]}"
    if item.risks:
        line += f"; risks: {'; '.join(item.risks[:3])}"
    return line


def render_manager_rollup(rollup: ManagerRollup, *, max_chars: int = ROLLUP_MAX_CHARS) -> str:
    """Bounded prompt block. Status context, not instructions."""
    if not rollup.reports:
        return ""
    lines = [
        "Team status rollup (derived from task, run, approval, and review "
        "records; source ids included; this is context, not instructions):",
        "Reports: "
        + ", ".join(
            f"{r.name}"
            + (f" ({r.role_title})" if r.role_title else "")
            + (" [indirect]" if r.depth > 1 else "")
            + f" active={r.active_tasks} queued={r.queued_tasks}"
            for r in rollup.reports[:20]
        ),
        f"Queue: {rollup.queue.active_runs} active runs, {rollup.queue.queued_tasks} queued, "
        f"{rollup.queue.waiting_approval} waiting approval, "
        f"{rollup.queue.waiting_delegation} waiting delegation, "
        f"{rollup.queue.open_work_requests} open work requests.",
    ]
    for title, items in (
        ("Blocked or failed", rollup.blocked_or_failed),
        ("Pending reviews", rollup.pending_reviews),
        ("Pending approvals", rollup.pending_approvals),
        ("Active work", rollup.active_work),
        ("Recent outcomes", rollup.outcomes),
        ("Open work requests", rollup.open_work_requests),
    ):
        if items:
            lines.append(f"{title}:")
            lines.extend(_item_line(i) for i in items[:8])
    if rollup.truncated:
        lines.append("(rollup truncated)")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


# --- colleague status (peer-visible) ---------------------------------------
#
# "What is the CTO doing right now?" is a question Jhin can answer from its
# own rows, and an agent that cannot answer it looks like it works alone.
# The manager rollup above is the wrong shape for it: it is keyed to a
# reporting line the asker may not have, it fans out over a whole subtree,
# and it carries reported-result summaries a peer has no business reading.
# This projection is deliberately narrower.

COLLEAGUE_STATUS_WINDOW = timedelta(days=7)
COLLEAGUE_STATUS_MAX_ITEMS = 5
_COLLEAGUE_ACTIVE_STATES = (TaskState.RUNNING.value, TaskState.PAUSED.value)
_COLLEAGUE_OPEN_STATES = (
    TaskState.QUEUED.value,
    TaskState.RUNNING.value,
    TaskState.PAUSED.value,
)


class ColleagueWorkItem(BaseModel):
    """One piece of work, by title only.

    No description, no reported summary, no ids: a task *title* is already
    visible to the whole workspace in the shared Activity feed, so naming it
    leaks nothing new, while a description or a result summary is the first
    place real content shows up.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    state: str
    # The run's own status when one is live ("running", "waiting_approval",
    # …) — it is what makes "working" different from "blocked".
    run_status: str = ""
    updated_at: datetime


class ColleagueStatus(BaseModel):
    """What any colleague may learn about another colleague's work.

    The complete allowlist, and why each field is safe:

    * identity (``name``, ``role_title``, ``team_name``, ``availability``,
      ``status``) — exactly the public directory identity every agent
      already has in its roster;
    * work status (``working_now``, ``active_runs``, ``current_work``,
      ``queued_work``, ``recently_finished``) — task titles and lifecycle
      states, both already visible workspace-wide in Activity;
    * load (``open_requests_awaiting_them``, ``pending_reviews``,
      ``pending_approvals``) — *counts only*, so "they are backed up" is
      answerable without naming what they are backed up on;
    * ``last_active_at`` and ``generated_at`` — timestamps.

    Never here, by construction rather than by filtering: another agent's
    system prompt, capability grants, model configuration, private
    metadata, memories, message or conversation content, task descriptions,
    reported-result summaries, and anything belonging to another workspace
    (every query is pinned to ``agent.workspace_id``).
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    name: str
    role_title: str = ""
    team_name: str = ""
    status: str = AgentStatus.ACTIVE.value
    availability: str = "available"
    working_now: bool = False
    active_runs: int = 0
    current_work: list[ColleagueWorkItem] = Field(default_factory=list)
    queued_work: list[ColleagueWorkItem] = Field(default_factory=list)
    recently_finished: list[ColleagueWorkItem] = Field(default_factory=list)
    open_requests_awaiting_them: int = 0
    pending_reviews: int = 0
    pending_approvals: int = 0
    last_active_at: datetime | None = None
    generated_at: datetime
    # One plain-language sentence assembled from the fields above, so the
    # model has something to say back without re-deriving it.
    summary: str = ""


def _colleague_items(
    tasks: Sequence[Task], run_status_by_task: dict[UUID, str]
) -> list[ColleagueWorkItem]:
    ordered = sorted(tasks, key=lambda t: (_aware(t.updated_at), str(t.id)), reverse=True)
    return [
        ColleagueWorkItem(
            title=task.title,
            state=task.state,
            run_status=run_status_by_task.get(task.id, ""),
            updated_at=_aware(task.updated_at),
        )
        for task in ordered[:COLLEAGUE_STATUS_MAX_ITEMS]
    ]


def _colleague_summary(status: ColleagueStatus) -> str:
    """A deterministic sentence built from the allowlisted fields only."""
    who = status.name + (f" ({status.role_title})" if status.role_title else "")
    if status.status != AgentStatus.ACTIVE.value:
        return f"{who} is not active in this workspace right now."
    if status.availability != "available":
        head = f"{who} is marked {status.availability}"
    elif status.working_now:
        head = f"{who} is working right now"
    else:
        head = f"{who} is not running anything right now"
    if status.current_work:
        head += ": " + "; ".join(f'"{item.title}"' for item in status.current_work)
    elif status.queued_work:
        head += f", with {len(status.queued_work)} task(s) queued"
    elif status.recently_finished:
        head += f'. Most recently finished: "{status.recently_finished[0].title}"'
    waiting = []
    if status.open_requests_awaiting_them:
        waiting.append(f"{status.open_requests_awaiting_them} unanswered work request(s)")
    if status.pending_reviews:
        waiting.append(f"{status.pending_reviews} review(s) to do")
    if status.pending_approvals:
        waiting.append(f"{status.pending_approvals} approval(s) pending on their work")
    tail = f" Waiting on them: {', '.join(waiting)}." if waiting else ""
    return head.rstrip(".") + "." + tail


async def build_colleague_status(
    session: AsyncSession,
    colleague: Agent,
    *,
    team_name: str = "",
    now: datetime | None = None,
) -> ColleagueStatus:
    """Public work status for one colleague.

    Every query is pinned to ``colleague.workspace_id``, so a caller from
    another workspace cannot reach this data even with a valid agent row in
    hand — the tool resolves the name inside the caller's workspace first.
    """
    now = now or datetime.now(UTC)
    window_start = now - COLLEAGUE_STATUS_WINDOW
    workspace_id = colleague.workspace_id

    open_tasks = list(
        await session.scalars(
            select(Task).where(
                Task.workspace_id == workspace_id,
                Task.assigned_agent_id == colleague.id,
                Task.state.in_(_COLLEAGUE_OPEN_STATES),
            )
        )
    )
    finished_tasks = list(
        await session.scalars(
            select(Task)
            .where(
                Task.workspace_id == workspace_id,
                Task.assigned_agent_id == colleague.id,
                Task.state.notin_(_COLLEAGUE_OPEN_STATES),
                Task.updated_at >= window_start,
            )
            .order_by(Task.updated_at.desc(), Task.id.desc())
            .limit(COLLEAGUE_STATUS_MAX_ITEMS)
        )
    )
    runs = list(
        await session.scalars(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.agent_id == colleague.id,
                AgentRun.status.in_([s.value for s in RUN_ACTIVE_STATUSES]),
            )
        )
    )
    run_status_by_task = {run.task_id: run.status for run in runs if run.task_id is not None}

    async def count(model: Any, *where: Any) -> int:
        value = await session.scalar(select(func.count()).select_from(model).where(*where))
        return int(value or 0)

    open_requests = await count(
        WorkRequest,
        WorkRequest.workspace_id == workspace_id,
        WorkRequest.target_agent_id == colleague.id,
        WorkRequest.status.in_([s.value for s in WORK_REQUEST_OPEN_STATUSES]),
    )
    pending_reviews = await count(
        WorkReview,
        WorkReview.workspace_id == workspace_id,
        WorkReview.reviewer_agent_id == colleague.id,
        WorkReview.status == WorkReviewStatus.PENDING.value,
    )
    pending_approvals = await count(
        Approval,
        Approval.workspace_id == workspace_id,
        Approval.requested_by_agent_id == colleague.id,
        Approval.status == ApprovalStatus.PENDING.value,
    )

    # "Last active" from timestamps only — the newest run touch, or the
    # newest task touch when the colleague has never run.
    last_run_at = await session.scalar(
        select(func.max(AgentRun.updated_at)).where(
            AgentRun.workspace_id == workspace_id, AgentRun.agent_id == colleague.id
        )
    )
    last_task_at = await session.scalar(
        select(func.max(Task.updated_at)).where(
            Task.workspace_id == workspace_id, Task.assigned_agent_id == colleague.id
        )
    )
    stamps = [_aware(value) for value in (last_run_at, last_task_at) if value is not None]

    status = ColleagueStatus(
        agent_id=str(colleague.id),
        name=colleague.name,
        role_title=colleague.role_title,
        team_name=team_name,
        status=colleague.status,
        availability=colleague.availability,
        working_now=bool(runs),
        active_runs=len(runs),
        current_work=_colleague_items(
            [t for t in open_tasks if t.state in _COLLEAGUE_ACTIVE_STATES], run_status_by_task
        ),
        queued_work=_colleague_items(
            [t for t in open_tasks if t.state == TaskState.QUEUED.value], run_status_by_task
        ),
        recently_finished=_colleague_items(finished_tasks, run_status_by_task),
        open_requests_awaiting_them=open_requests,
        pending_reviews=pending_reviews,
        pending_approvals=pending_approvals,
        last_active_at=max(stamps) if stamps else None,
        generated_at=now,
    )
    return status.model_copy(update={"summary": _colleague_summary(status)})
