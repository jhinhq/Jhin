"""Closing ``sandbox_job`` rows whose runner is demonstrably gone.

A ``sandbox_job`` row is opened by the tool worker before the job is
submitted and closed by the same coroutine when the job ends. That is the
only writer, so when the worker is killed mid-job the row stays ``running``
and nothing in the product will ever look at it again: no timer, no retry, no
person. One such row is what a redeploy left behind, and it would have sat
there until the database was dropped.

The shutdown path (``jhin_tool_worker.drain`` and the cancellation branch in
the CLI connector) is what stops new orphans being made. This is the other
half — the sweep for the ones that already exist, and for the cases no
shutdown path can cover: a SIGKILL, a power cut, a worker that died between
two statements.

**What makes a job demonstrably gone.** Two independent facts, and the sweep
insists on both:

1. *The job is past its own deadline.* Every job carries the timeout the
   runner enforces on it. A row whose ``started_at`` plus that timeout, plus
   a grace, is in the past describes a container the runner would already
   have killed. Before that moment a row in ``running`` is simply a job that
   is running, and the sweep does not touch it.
2. *The runner says so.* The runner's job registry lives in memory: it holds
   every job this runner process has seen and nothing from before its last
   restart. Asking it about the job distinguishes the cases that matter, and
   the sweep does something different for each:

   * a terminal status — the runner *does* know, and knows how it ended.
     Close it with the outcome the runner reports, which is better than any
     guess: the exit code, the duration, the log tails.
   * ``running`` — it is genuinely still going. Leave it completely alone.
   * ``404`` — this runner has no record of the job, which is **two different
     facts wearing one answer**, and the sweep asks a second question before
     it acts on either. ``GET /v1/runner/memory`` says when the runner began
     serving and how long it keeps a finished job's record:

     - the row started *before* the runner began serving: that container
       belonged to a previous incarnation and a runner's first act on startup
       is to force-remove those, so nothing is running it and no outcome is
       coming. ``runner_gone``.
     - the runner was already serving when the row started, and a record of a
       job it finished would still be inside its retention window: then it
       never received this job at all — the worker died between committing the
       row and submitting — and again nothing ran and nothing is coming.
       ``runner_gone``.
     - otherwise the record may have existed and been let go. The job is over
       (rule 1 says its deadline has passed, and the runner is not running it)
       but *how* it ended is no longer recoverable from anyone, and saying
       "gone" would be a claim about a container that may well have completed.
       Closed as ``outcome_forgotten`` instead, which says exactly that.

     This is what a retention window alone could not do. The window is sized
     against this sweep's grace and its interval — but not against the sweep
     being *absent*, and no finite window can be: a worker that crash-loops or
     stays down for an afternoon comes back and asks about rows hours past
     their deadline, for jobs the runner finished and dropped, and every one
     of them used to be written down as ``runner_gone``. The runner's own
     account of what its memory covers is what removes the guess.

   Anything else, including the runner being unreachable — or unable to say
   what it remembers — is "I could not ask", which is not the same as "it is
   not there" and never closes a row on a 404. A terminal answer still closes
   one: that needs no memory boundary, because the runner is holding the
   outcome as it speaks.

This assumes one runner behind ``SANDBOX_RUNNER_URL``, which is what the
compose topology gives: a single ``sandbox-runner`` service on the ``runner``
network, reachable only by the tool worker. Behind a pool of runners a 404
from one would prove nothing, and rule 1 alone would not be enough.

**What it deliberately does not do.** It does not touch ``tool_call``. That
row's state machine is the gateway's, and it is what the at-most-once
guarantee is written in; a sweep reaching in to rewrite a terminal tool call
would be exactly the kind of second writer this module exists because of.
Recovering a tool call is the gateway's job, on the next attempt, using the
tool's own classification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_connectors.cli.output import sanitized_output_tail
from jhin_connectors.cli.runner_client import SandboxRunnerError, runner_memory
from jhin_connectors.cli.runner_client import sandbox_job_state as runner_job_state
from jhin_db.models import AuditEvent, SandboxJob
from jhin_domain import ActorType, SandboxJobStatus

#: Extra time allowed past a job's own timeout before the sweep will consider
#: it overdue.
#:
#: The row's ``started_at`` is stamped by the worker before it submits, and
#: the job's ``timeout_seconds`` is the *container's* clock. Between the two
#: the runner may spend its whole pre-start budget — queuing for the workspace
#: volume and walking it to measure, up to ninety seconds — so the grace has
#: to cover that as well as the poll interval, the kill, and collecting the
#: logs afterwards. Two minutes covered the second list and not the first,
#: which put a live job's deadline and this sweep's within thirty seconds of
#: each other.
#:
#: Generous on purpose, and the asymmetry is the reason: waiting another few
#: minutes to close a row costs nothing at all, and closing a live job's row
#: costs a tool call that gets two answers.
DEFAULT_OVERDUE_GRACE_SECONDS = 300
#: Rows examined per sweep. A bound, not a target: orphans arrive one redeploy
#: at a time, and a sweep that walked an unbounded table would be a new way to
#: hurt the database.
DEFAULT_SWEEP_LIMIT = 200
#: What a job nothing is running any more, and nothing ever finished, is
#: recorded as. It names the evidence rather than a guess about the container:
#: either the runner started after this job did (so the container was reaped),
#: or the runner has been serving throughout and would still be holding a
#: record if this job had ever reached it.
RUNNER_GONE_CODE = "runner_gone"
#: What a job whose *outcome* is no longer recoverable is recorded as. The job
#: is over — its deadline passed and no runner is running it — but the process
#: that watched it has let go of what it did, so neither "it completed" nor
#: "it is gone" would be true. Its own code, because an operator reading the
#: table afterwards is entitled to know which of those the row is.
OUTCOME_FORGOTTEN_CODE = "outcome_forgotten"

#: Injected for tests; the defaults are the real internal runner API.
JobStateProbe = Callable[[str], Awaitable[dict[str, Any] | None]]
MemoryProbe = Callable[[], Awaitable[dict[str, Any]]]

#: The two ways a 404 can be proof that nothing ran and nothing will report.
#: Spelled as a type rather than as two strings, because they are compared by
#: name in exactly one other place and a typo there would silently mean
#: "cannot prove".
_Absence = Literal["reaped", "never_submitted"]

_TERMINAL_RUNNER_STATUSES = frozenset(
    {
        SandboxJobStatus.COMPLETED.value,
        SandboxJobStatus.FAILED.value,
        SandboxJobStatus.TIMEOUT.value,
        SandboxJobStatus.CANCELLED.value,
    }
)
_MAX_TAIL_CHARS = 4_000


@dataclass(frozen=True)
class ReconciledJob:
    """One row the sweep closed, and what it closed it as."""

    job_id: UUID
    status: str
    error_code: str | None
    evidence: str


@dataclass(frozen=True)
class SweepReport:
    examined: int
    closed: tuple[ReconciledJob, ...]
    still_running: tuple[UUID, ...]
    unprovable: tuple[UUID, ...]

    def describe(self) -> str:
        lines = [
            f"examined {self.examined} overdue running job(s)",
            f"closed {len(self.closed)}",
            f"left running {len(self.still_running)}",
            f"could not prove {len(self.unprovable)}",
        ]
        for job in self.closed:
            lines.append(f"  {job.job_id} -> {job.status} ({job.error_code or 'no error'})")
        for job_id in self.still_running:
            lines.append(f"  {job_id} left alone: the runner says it is still running")
        for job_id in self.unprovable:
            # Two silences, one line: the runner could not be asked, or it
            # answered 404 and would not say whether that 404 covers this job.
            lines.append(f"  {job_id} left alone: nothing the runner said settles it")
        return "\n".join(lines)


def _tail(value: object) -> str:
    return sanitized_output_tail(value, max_chars=_MAX_TAIL_CHARS)


async def _overdue_running_jobs(
    session: AsyncSession,
    *,
    now: datetime,
    grace_seconds: int,
    limit: int,
) -> list[tuple[SandboxJob, datetime]]:
    """Running rows whose own deadline has passed, each with the aware
    ``started_at`` the deadline was computed from.

    The moment travels with the row because the caller needs the same one: it
    is what a 404 is then measured against, and normalising it twice is how
    two comparisons of one fact come to disagree.

    The deadline is computed in Python from each row rather than in SQL,
    because ``timeout_seconds`` is per row and the comparison has to be
    "``started_at`` + this row's timeout", not a single cutoff for all of
    them. The query narrows to running rows old enough that *no* timeout
    could still be open, and the exact test is applied to what comes back.
    """
    horizon = now - timedelta(seconds=grace_seconds)
    rows = (
        await session.scalars(
            select(SandboxJob)
            .where(
                or_(
                    SandboxJob.status == SandboxJobStatus.RUNNING.value,
                    and_(
                        SandboxJob.status == SandboxJobStatus.FAILED.value,
                        SandboxJob.error_code == "runner_error",
                    ),
                ),
                SandboxJob.started_at.is_not(None),
                SandboxJob.started_at < horizon,
            )
            .order_by(SandboxJob.started_at)
            .limit(limit)
        )
    ).all()
    overdue: list[tuple[SandboxJob, datetime]] = []
    for row in rows:
        started = row.started_at
        if started is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if started + timedelta(seconds=row.timeout_seconds + grace_seconds) <= now:
            overdue.append((row, started))
    return overdue


@dataclass(frozen=True)
class _RunnerMemory:
    """What the runner said its memory covers, as this pass will use it.

    Read once per pass and read *before* any job is probed, which is the safe
    order: ``serving_since`` only moves forward, so a runner that restarts
    mid-pass makes this statement older than the answers it explains — and an
    older statement can only make the sweep more cautious, never less.

    The comparison below puts a moment this worker stamped beside a moment the
    runner stamped, on the same deployment assumption the re-dispatch interlock
    already states: both are containers on one Docker host and read one clock
    (``docs/architecture/sandboxing.md``).
    """

    serving_since: datetime
    retention_seconds: float

    @classmethod
    def parse(cls, document: dict[str, Any]) -> _RunnerMemory:
        moment = datetime.fromisoformat(str(document["serving_since"]))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return cls(
            serving_since=moment,
            retention_seconds=float(document["job_record_retention_seconds"]),
        )

    def absence_of(
        self, *, started: datetime, timeout_seconds: int, now: datetime
    ) -> _Absence | None:
        """What a 404 about this row proves, given what the runner remembers.

        ``"reaped"`` and ``"never_submitted"`` are both proof that nothing is
        running and nothing will report; ``None`` is the third case, where the
        outcome existed and this runner is simply no longer a witness to it.
        """
        if started < self.serving_since:
            return "reaped"
        if now < started + timedelta(seconds=timeout_seconds + self.retention_seconds):
            return "never_submitted"
        return None


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _absence_closure(
    memory: _RunnerMemory | None,
    *,
    started: datetime,
    timeout_seconds: int,
    now: datetime,
) -> tuple[dict[str, Any], str, str] | None:
    """(column values, audit action, evidence) for a row the runner answered
    404 for, or ``None`` when the 404 cannot be interpreted at all.

    ``None`` is not "leave it running": it is "I could not establish what this
    silence means", which is the same answer as an unreachable runner and is
    counted with it.
    """
    if memory is None:
        return None
    absence = memory.absence_of(started=started, timeout_seconds=timeout_seconds, now=now)
    if absence == "reaped":
        return (
            {
                "status": SandboxJobStatus.FAILED.value,
                "error_code": RUNNER_GONE_CODE,
                "stderr_tail": (
                    "the sandbox runner has no record of this job and started after the "
                    "job did, so its container was force-removed by that startup and no "
                    "outcome will ever be reported"
                ),
            },
            "sandbox.job.failed",
            (
                "the runner answered 404 for this job id after its timeout had passed, "
                f"and has only been serving since {_moment(memory.serving_since)} — after "
                "this job started"
            ),
        )
    if absence == "never_submitted":
        return (
            {
                "status": SandboxJobStatus.FAILED.value,
                "error_code": RUNNER_GONE_CODE,
                "stderr_tail": (
                    "the sandbox runner has been serving since before this job started "
                    "and has no record of it, so the job was never submitted and nothing "
                    "ran"
                ),
            },
            "sandbox.job.failed",
            (
                "the runner answered 404 for this job id while still inside the window in "
                "which it would be holding the record of a job it had finished, so this "
                "job never reached it"
            ),
        )
    return (
        {
            "status": SandboxJobStatus.FAILED.value,
            # Not a claim about the container. The row is closed because the job
            # is certainly over — its deadline has passed and no runner is
            # running it — while what it did is no longer recoverable from
            # anything: this runner was serving when the job began, so it did
            # receive it, and it has since let the record go. ``status`` has no
            # word for that, so the code carries it and the tail says it in full.
            "error_code": OUTCOME_FORGOTTEN_CODE,
            "stderr_tail": (
                "this job is over — its deadline has passed and no sandbox runner is "
                "running it — but the runner that watched it has let go of what it did, "
                "so its outcome cannot be recovered. It may well have completed"
            ),
        },
        "sandbox.job.failed",
        (
            "the runner answered 404 for this job id, and the job is old enough that a "
            "record of it would have been dropped, so the 404 is not evidence that "
            "nothing ran"
        ),
    )


def _closure_for(state: dict[str, Any]) -> tuple[dict[str, Any], str, str] | None:
    """(column values, audit action, evidence) for a row the runner still holds.

    ``None`` means leave the row alone: the runner says the job is still
    running, or answered something this function will not interpret. Guessing
    is not on the list of options.
    """
    status = str(state.get("status", ""))
    if status not in _TERMINAL_RUNNER_STATUSES:
        return None
    completed = (
        SandboxJobStatus.COMPLETED.value if status == SandboxJobStatus.COMPLETED.value else status
    )
    values: dict[str, Any] = {
        "status": completed,
        "error_code": None if status == SandboxJobStatus.COMPLETED.value else status,
        "stdout_tail": _tail(state.get("stdout")),
        "stderr_tail": _tail(state.get("stderr")),
    }
    exit_code = state.get("exit_code")
    if isinstance(exit_code, int):
        values["exit_code"] = exit_code
    duration = state.get("duration_ms")
    if isinstance(duration, int):
        values["duration_ms"] = duration
    action = (
        "sandbox.job.completed"
        if status == SandboxJobStatus.COMPLETED.value
        else "sandbox.job.failed"
    )
    return values, action, f"the runner still holds this job and reports it as '{status}'"


async def _runner_memory(memory_probe: MemoryProbe) -> _RunnerMemory | None:
    """What the runner remembers, or ``None`` if it would not say.

    Asked once per pass and before any job, so the statement is never newer
    than the answers it explains. ``None`` costs the pass only its 404s: a
    runner that still holds a job answers with the outcome, and that needs no
    boundary. A runner too old to have the endpoint lands here too, which is
    the right way round — it is the deployment order this release documents,
    and the cost of getting it wrong is a row left open rather than a row
    closed wrongly.
    """
    try:
        return _RunnerMemory.parse(await memory_probe())
    except (SandboxRunnerError, KeyError, TypeError, ValueError):
        return None


async def reconcile_orphaned_sandbox_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    probe: JobStateProbe = runner_job_state,
    memory_probe: MemoryProbe = runner_memory,
    now: datetime | None = None,
    grace_seconds: int = DEFAULT_OVERDUE_GRACE_SECONDS,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> SweepReport:
    """Close every overdue ``running`` job whose ending the runner can be shown
    to have decided or lost, and leave every other one exactly as it is."""
    moment = now or datetime.now(UTC)
    async with session_factory() as session:
        candidates = await _overdue_running_jobs(
            session,
            now=moment,
            grace_seconds=grace_seconds,
            limit=limit,
        )
        pending = [
            (
                row.id,
                row.workspace_id,
                row.run_id,
                row.tool_call_id,
                row.image,
                row.network_policy,
                started,
                row.timeout_seconds,
                row.status,
                row.error_code,
            )
            for row, started in candidates
        ]

    closed: list[ReconciledJob] = []
    live: list[UUID] = []
    unprovable: list[UUID] = []
    memory = await _runner_memory(memory_probe) if pending else None
    for (
        job_id,
        workspace_id,
        run_id,
        tool_call_id,
        image,
        network_policy,
        started,
        timeout_seconds,
        observed_status,
        observed_error,
    ) in pending:
        try:
            state = await probe(str(job_id))
        except SandboxRunnerError:
            # Could not ask. Not the same as an answer, and never a reason to
            # close a row: an unreachable runner is exactly the moment a live
            # job would look dead.
            unprovable.append(job_id)
            continue
        if state is None:
            closure = _absence_closure(
                memory,
                started=started,
                timeout_seconds=timeout_seconds,
                now=moment,
            )
            if closure is None:
                # A 404 nobody can interpret. The runner would not say what its
                # memory covers, so "it never heard of this job" and "it heard,
                # finished, and forgot" are still one answer here.
                unprovable.append(job_id)
                continue
        else:
            closure = _closure_for(state)
            if closure is None:
                live.append(job_id)
                continue
        values, action, evidence = closure
        async with session_factory() as session:
            # Compare the observed state and error classification. Legacy
            # runner_error rows were incorrectly marked failed on a lost
            # connection; a newer confirmed outcome must still win this race.
            updated = await session.scalar(
                sa_update(SandboxJob)
                .where(
                    SandboxJob.id == job_id,
                    SandboxJob.workspace_id == workspace_id,
                    SandboxJob.status == observed_status,
                    SandboxJob.error_code == observed_error,
                )
                .values(completed_at=moment, **values)
                .returning(SandboxJob.id)
                .execution_options(synchronize_session=False)
            )
            if updated is None:
                live.append(job_id)
                await session.rollback()
                continue
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    action=action,
                    target_type="sandbox_job",
                    target_id=job_id,
                    metadata_json={
                        "run_id": str(run_id) if run_id else None,
                        "tool_call_id": str(tool_call_id) if tool_call_id else None,
                        "image": image,
                        "network_policy": network_policy,
                        "status": values["status"],
                        "reconciled": True,
                        "evidence": evidence,
                    },
                )
            )
            await session.commit()
        closed.append(
            ReconciledJob(
                job_id=job_id,
                status=str(values["status"]),
                error_code=values.get("error_code"),
                evidence=evidence,
            )
        )
    return SweepReport(
        examined=len(pending),
        closed=tuple(closed),
        still_running=tuple(live),
        unprovable=tuple(unprovable),
    )


async def sandbox_reconcile_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stop: asyncio.Event,
    interval_seconds: float,
    probe: JobStateProbe = runner_job_state,
    memory_probe: MemoryProbe = runner_memory,
) -> None:
    """Sweep on startup and then on an interval, until ``stop`` is set.

    **On startup** is the pass that has to be right about a worker that has
    been away. It runs against rows that may be hours past their deadline —
    that is what "the worker was down" leaves behind — and those are exactly
    the ones the runner has finished and forgotten. What it does with them is
    decided in :func:`_absence_closure` against the runner's own account of
    its memory, not against how long this process has been gone.

    Never raises. A sweep that cannot run is a sweep that runs next time —
    it must not be able to take the worker down, and it must not be able to
    delay a shutdown, which is why the wait between passes is a wait on the
    stop event rather than a sleep.

    It writes no log records. Closing a job is a durable fact, so it is
    recorded where durable facts about jobs already live: an ``audit_event``
    row against the job, carrying the evidence the closure rested on.
    """
    while not stop.is_set():
        try:
            await reconcile_orphaned_sandbox_jobs(
                session_factory, probe=probe, memory_probe=memory_probe
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        with suppress(TimeoutError):
            async with asyncio.timeout(interval_seconds):
                await stop.wait()


__all__ = [
    "DEFAULT_OVERDUE_GRACE_SECONDS",
    "DEFAULT_SWEEP_LIMIT",
    "OUTCOME_FORGOTTEN_CODE",
    "RUNNER_GONE_CODE",
    "ReconciledJob",
    "SweepReport",
    "reconcile_orphaned_sandbox_jobs",
    "sandbox_reconcile_loop",
]
