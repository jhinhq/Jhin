"""Leaving cleanly, and clearing up after a leave that was not clean.

Two halves of the same problem. :mod:`jhin_tool_worker.drain` is what a
SIGTERM'd worker does with the seconds Docker gives it before SIGKILL:
refuse new work, finish what is in flight. :mod:`jhin_tool_worker.
sandbox_reconcile` is for the rows that get left behind anyway — a SIGKILL, a
power cut, a job that outlived the budget — and its whole job is to close
those *without ever closing one that is still running*.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.exceptions import ApplicationError

from jhin_connectors.cli.runner_client import SandboxRunnerError
from jhin_db.base import Base
from jhin_db.models import AuditEvent, SandboxJob, Workspace
from jhin_domain import SandboxJobStatus, new_uuid7
from jhin_tool_worker.drain import DRAINING_ERROR_TYPE, WorkerDrain
from jhin_tool_worker.sandbox_reconcile import (
    DEFAULT_OVERDUE_GRACE_SECONDS,
    OUTCOME_FORGOTTEN_CODE,
    RUNNER_GONE_CODE,
    reconcile_orphaned_sandbox_jobs,
)

JOB_TIMEOUT = 300
RETENTION = 3600.0


# --- draining ---------------------------------------------------------------


def test_a_drain_refuses_new_work_before_anything_is_claimed() -> None:
    drain = WorkerDrain()
    drain.begin()
    with pytest.raises(ApplicationError) as raised, drain.hold():
        pytest.fail("the body must not run once the worker is leaving")
    assert raised.value.type == DRAINING_ERROR_TYPE
    # Retryable: the work is fine, this process is not.
    assert raised.value.non_retryable is False
    assert "nothing ran" in str(raised.value)


async def test_a_drain_waits_for_work_that_is_already_running() -> None:
    drain = WorkerDrain()
    released = asyncio.Event()

    async def in_flight() -> None:
        with drain.hold():
            await released.wait()

    task = asyncio.create_task(in_flight())
    await asyncio.sleep(0)
    assert drain.in_flight == 1

    drain.begin()
    still_running = await drain.wait_idle(0.05)
    assert still_running == 1

    released.set()
    await task
    assert await drain.wait_idle(1.0) == 0


def test_an_undrained_worker_counts_but_refuses_nothing() -> None:
    """The default in every unit test and direct caller: behaves exactly as
    the worker did before draining existed."""
    drain = WorkerDrain()
    with drain.hold():
        assert drain.in_flight == 1
    assert drain.in_flight == 0


def test_a_hold_that_raises_still_releases_its_count() -> None:
    drain = WorkerDrain()
    with pytest.raises(RuntimeError), drain.hold():
        raise RuntimeError("the tool failed")
    assert drain.in_flight == 0


# --- reconciling ------------------------------------------------------------


class _World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], workspace_id: UUID) -> None:
        self.sessions = sessions
        self.workspace_id = workspace_id

    async def job(self, *, age_seconds: float, status: str = "running") -> UUID:
        started = datetime.now(UTC) - timedelta(seconds=age_seconds)
        job_id = new_uuid7()
        async with self.sessions() as session:
            session.add(
                SandboxJob(
                    id=job_id,
                    workspace_id=self.workspace_id,
                    status=status,
                    image="jhin-sandbox:latest",
                    command="list .",
                    network_policy="none",
                    timeout_seconds=JOB_TIMEOUT,
                    started_at=started,
                )
            )
            await session.commit()
        return job_id

    async def row(self, job_id: UUID) -> SandboxJob:
        async with self.sessions() as session:
            row = await session.get(SandboxJob, job_id)
            assert row is not None
            return row

    async def audit(self, job_id: UUID) -> list[AuditEvent]:
        async with self.sessions() as session:
            return list(
                await session.scalars(select(AuditEvent).where(AuditEvent.target_id == job_id))
            )


@pytest.fixture
async def world(tmp_path: Any) -> AsyncIterator[_World]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as setup:
        workspace = Workspace(name="Reconcile", slug=f"reconcile-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.commit()
        workspace_id = workspace.id
    yield _World(sessions, workspace_id)
    await engine.dispose()


def _forgotten() -> Any:
    async def probe(job_id: str) -> dict[str, Any] | None:
        return None

    return probe


def _memory(*, serving_for_seconds: float, retention: float = RETENTION) -> Any:
    """A runner that has been serving for ``serving_for_seconds``.

    Every 404 below is read against this: the same silence means "your job's
    container was reaped when I started", "you never sent it to me", or "I have
    let go of what it did", and which one it is decides what gets written down.
    """
    document = {
        "serving_since": (datetime.now(UTC) - timedelta(seconds=serving_for_seconds)).isoformat(),
        "job_record_retention_seconds": retention,
    }

    async def probe() -> dict[str, Any]:
        return document

    return probe


def _memory_unreachable() -> Any:
    async def probe() -> dict[str, Any]:
        raise SandboxRunnerError("sandbox runner unreachable: ConnectError")

    return probe


def _answers(document: dict[str, Any]) -> Any:
    async def probe(job_id: str) -> dict[str, Any] | None:
        return document

    return probe


def _unreachable() -> Any:
    async def probe(job_id: str) -> dict[str, Any] | None:
        raise SandboxRunnerError("sandbox runner unreachable: ConnectError")

    return probe


_OVERDUE = JOB_TIMEOUT + DEFAULT_OVERDUE_GRACE_SECONDS + 60


async def test_a_job_whose_runner_started_after_it_is_closed_as_gone(world: _World) -> None:
    """The original case: the runner restarted under a live job, and a runner's
    first act on startup is to force-remove the containers of its last life."""
    job_id = await world.job(age_seconds=_OVERDUE)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory(serving_for_seconds=60),
    )

    row = await world.row(job_id)
    assert (row.status, row.error_code) == (SandboxJobStatus.FAILED.value, RUNNER_GONE_CODE)
    assert row.completed_at is not None
    assert [job.job_id for job in report.closed] == [job_id]
    # The closure carries the evidence it rested on, for whoever reads it later.
    [event] = await world.audit(job_id)
    assert event.metadata_json["reconciled"] is True
    assert "404" in event.metadata_json["evidence"]
    assert "only been serving since" in event.metadata_json["evidence"]


async def test_a_job_the_runner_never_received_is_closed_as_gone(world: _World) -> None:
    """The other half of ``runner_gone``, and it needs no restart: the runner
    has been up all along and would still be holding this job's record if it
    had ever run it, so the worker died between committing the row and
    submitting. Nothing ran."""
    job_id = await world.job(age_seconds=_OVERDUE)

    await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory(serving_for_seconds=86_400),
    )

    row = await world.row(job_id)
    assert (row.status, row.error_code) == (SandboxJobStatus.FAILED.value, RUNNER_GONE_CODE)
    assert "never submitted" in row.stderr_tail
    [event] = await world.audit(job_id)
    assert "still inside the window" in event.metadata_json["evidence"]


async def test_a_job_the_runner_finished_and_forgot_is_not_called_gone(world: _World) -> None:
    """The regression this contract exists for.

    A worker that is down longer than the runner's retention window — a crash
    loop, or a stack stopped for an afternoon — comes back and sweeps rows
    hours past their deadline. The runner has long since finished those jobs
    and dropped the records, so it answers 404 for every one of them, and the
    sweep used to write ``runner_gone`` over jobs that had completed. The job
    is over either way, but *how* it ended is no longer knowable, and the row
    now says exactly that instead.
    """
    job_id = await world.job(age_seconds=JOB_TIMEOUT + RETENTION + 600)

    await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory(serving_for_seconds=7 * 86_400),
    )

    row = await world.row(job_id)
    assert (row.status, row.error_code) == (
        SandboxJobStatus.FAILED.value,
        OUTCOME_FORGOTTEN_CODE,
    )
    assert "may well have completed" in row.stderr_tail
    [event] = await world.audit(job_id)
    assert "not evidence that nothing ran" in event.metadata_json["evidence"]


async def test_a_404_proves_nothing_when_the_runner_will_not_say_what_it_remembers(
    world: _World,
) -> None:
    """The boundary is the runner's to state, and a sweep that cannot get it —
    an older runner, a runner that answered oddly — has a 404 it cannot read.
    That is "I could not ask", not "it is not there"."""
    job_id = await world.job(age_seconds=_OVERDUE)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory_unreachable(),
    )

    assert (await world.row(job_id)).status == SandboxJobStatus.RUNNING.value
    assert report.unprovable == (job_id,)
    assert report.closed == ()
    assert await world.audit(job_id) == []


async def test_a_runner_that_kept_the_outcome_needs_no_memory_statement(world: _World) -> None:
    """A terminal answer is the runner holding the outcome as it speaks, so it
    closes the row whatever the memory endpoint does."""
    job_id = await world.job(age_seconds=_OVERDUE)

    await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_answers({"status": "completed", "exit_code": 0}),
        memory_probe=_memory_unreachable(),
    )

    row = await world.row(job_id)
    assert (row.status, row.exit_code) == (SandboxJobStatus.COMPLETED.value, 0)


async def test_a_job_the_runner_says_is_running_is_left_completely_alone(
    world: _World,
) -> None:
    """The one thing the sweep must never get wrong. Overdue by the clock is
    not the same as finished, and the runner is the only authority on which."""
    job_id = await world.job(age_seconds=_OVERDUE)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_answers({"status": "running"}),
        memory_probe=_memory(serving_for_seconds=60),
    )

    assert (await world.row(job_id)).status == SandboxJobStatus.RUNNING.value
    assert report.closed == ()
    assert report.still_running == (job_id,)
    assert await world.audit(job_id) == []


async def test_a_runner_that_cannot_be_asked_closes_nothing(world: _World) -> None:
    """ "I could not ask" is not "it is not there" — and an unreachable runner
    is exactly the moment a live job would look dead."""
    job_id = await world.job(age_seconds=_OVERDUE)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_unreachable(),
        memory_probe=_memory(serving_for_seconds=60),
    )

    assert (await world.row(job_id)).status == SandboxJobStatus.RUNNING.value
    assert report.unprovable == (job_id,)


async def test_a_job_inside_its_own_deadline_is_never_examined(world: _World) -> None:
    """A running job is a running job. The age guard is the half of the proof
    that does not depend on anything answering."""
    job_id = await world.job(age_seconds=30)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory(serving_for_seconds=60),
    )

    assert report.examined == 0
    assert (await world.row(job_id)).status == SandboxJobStatus.RUNNING.value


async def test_the_runners_own_outcome_is_recorded_when_it_still_has_one(
    world: _World,
) -> None:
    """A runner that kept the record knows more than the sweep can infer, so
    the sweep writes down what it says rather than a guess."""
    job_id = await world.job(age_seconds=_OVERDUE)

    await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_answers(
            {
                "status": "timeout",
                "exit_code": 137,
                "duration_ms": 300_000,
                "stdout": "cloning\n",
                "stderr": "killed\n",
            }
        ),
        memory_probe=_memory(serving_for_seconds=86_400),
    )

    row = await world.row(job_id)
    assert (row.status, row.error_code, row.exit_code) == (
        SandboxJobStatus.TIMEOUT.value,
        SandboxJobStatus.TIMEOUT.value,
        137,
    )
    assert (row.stdout_tail, row.stderr_tail) == ("cloning\n", "killed\n")


async def test_a_row_the_owning_worker_closed_first_is_not_overwritten(
    world: _World,
) -> None:
    """The guard on ``running``: if the worker came back and finished the job
    between the query and the write, its outcome is the true one."""
    job_id = await world.job(age_seconds=_OVERDUE)

    async def probe_then_finish(job_id_text: str) -> dict[str, Any] | None:
        async with world.sessions() as session:
            row = await session.get(SandboxJob, UUID(job_id_text))
            assert row is not None
            row.status = SandboxJobStatus.COMPLETED.value
            row.exit_code = 0
            row.completed_at = datetime.now(UTC)
            await session.commit()
        return None

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=probe_then_finish,
        memory_probe=_memory(serving_for_seconds=60),
    )

    row = await world.row(job_id)
    assert (row.status, row.exit_code) == (SandboxJobStatus.COMPLETED.value, 0)
    assert report.closed == ()
    assert await world.audit(job_id) == []


async def test_a_terminal_row_is_not_a_candidate(world: _World) -> None:
    job_id = await world.job(age_seconds=_OVERDUE, status=SandboxJobStatus.COMPLETED.value)

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions,
        probe=_forgotten(),
        memory_probe=_memory(serving_for_seconds=60),
    )

    assert report.examined == 0
    assert (await world.row(job_id)).status == SandboxJobStatus.COMPLETED.value


@pytest.mark.parametrize("runner_status", ["running", "completed", "unreachable"])
async def test_legacy_runner_error_requires_a_real_runner_outcome(world, runner_status):
    job_id = await world.job(age_seconds=_OVERDUE, status="failed")
    async with world.sessions() as db:
        row = await db.get(SandboxJob, job_id)
        row.error_code = "runner_error"
        await db.commit()
    probe = (
        _unreachable()
        if runner_status == "unreachable"
        else _answers({"status": runner_status, "stdout": "actual output", "exit_code": 0})
    )
    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions, probe=probe, memory_probe=_memory_unreachable()
    )
    row = await world.row(job_id)
    assert report.examined == 1
    if runner_status == "completed":
        assert row.status == "completed" and row.error_code is None
        assert row.stdout_tail == "actual output"
    else:
        assert row.status == "failed" and row.error_code == "runner_error"
        assert not report.closed


async def test_legacy_reconcile_does_not_overwrite_a_new_confirmed_failure(world):
    job_id = await world.job(age_seconds=_OVERDUE, status="failed")
    async with world.sessions() as db:
        row = await db.get(SandboxJob, job_id)
        row.error_code = "runner_error"
        await db.commit()

    async def probe(_):
        async with world.sessions() as db:
            row = await db.get(SandboxJob, job_id)
            row.error_code = "confirmed_failed"
            await db.commit()
        return {"status": "completed", "stdout": "stale", "exit_code": 0}

    report = await reconcile_orphaned_sandbox_jobs(
        world.sessions, probe=probe, memory_probe=_memory_unreachable()
    )
    assert not report.closed
    assert (await world.row(job_id)).error_code == "confirmed_failed"
