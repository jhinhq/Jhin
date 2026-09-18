"""What the runner keeps, for how long, and what it says once it stops.

The invocation ledger is an index into ``_jobs``, so ``_jobs`` is the store
that has to be bounded — and it was not. Nothing evicted, nothing capped,
nothing swept: forty ordinary jobs held about 340 KB apiece for the life of
the process, and a job forty jobs old still served every byte its container
had printed. Bounding it is not just a matter of deleting old things, because
two other promises are made out of exactly this memory:

* **the tool worker's reconciliation sweep** closes a ``sandbox_job`` row
  whose worker died by asking this runner about the job, and reads a 404 as
  "gone, no outcome is coming". A record dropped before that sweep can reach
  it turns a job that *completed* into ``runner_gone``;
* **the ledger's inference** — "I hold no record of that invocation, so it was
  never submitted here" — is only sound while the runner remembers everything
  since it started. Forgetting quietly would turn that inference back into the
  one that repeats an effect.

So the two bounds are shaped by those promises rather than by a number: the
large thing (captured output) goes first and by count, the small thing (the
record, and with it the ledger entry) goes much later and says how far back
its memory now reaches.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from jhin_sandbox_runner.jobs import (
    _OUTPUT_RELEASED_MARKER,
    InvocationOutcomeUnknownError,
    JobManager,
    _Dispatch,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

pytestmark = pytest.mark.anyio

_INVOCATION = "11111111-1111-7111-8111-111111111111"
_RETENTION_SECONDS = 3600.0


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "sandbox_runner_token": "test-token",
        "sandbox_docker_mode": "rootless",
        "sandbox_docker_transport_url": "http://rootless-docker-transport:2375",
        "sandbox_job_record_retention_seconds": _RETENTION_SECONDS,
    }
    values.update(overrides)
    return Settings(**values)


class _Container:
    """A container that exits on its first poll and prints its own name."""

    def __init__(self, name: str) -> None:
        self.id = f"container-{name}"
        self.name = name

    async def start(self) -> None:
        return None

    async def show(self) -> dict[str, dict[str, Any]]:
        return {"State": {"Running": False, "ExitCode": 0}}

    async def kill(self) -> None:
        return None

    async def log(self, **_kwargs: bool):
        yield f"output of {self.name}\n"

    async def delete(self, *, force: bool, v: bool) -> None:
        del force, v


class _Containers:
    async def create(self, _config: dict[str, Any], *, name: str) -> _Container:
        return _Container(name)

    def container(self, container_id: str) -> _Container:
        return _Container(container_id)


class _Docker:
    def __init__(self) -> None:
        self.containers = _Containers()


def _manager(**overrides: Any) -> JobManager:
    manager = JobManager(_settings(**overrides))
    manager._docker = cast(Any, _Docker())
    # A runner that has been up for a day, because that is the only kind that
    # can have forgotten anything: everything here is about dispatches this
    # process accepted and then let go of, which is a different silence from
    # the ones it never saw.
    manager._serving_since = datetime.now(UTC) - timedelta(days=1)
    return manager


def _request(index: int, **overrides: Any) -> SandboxJobRequest:
    values: dict[str, Any] = {
        "job_id": f"{index:012x}",
        "command": ["bash", "-c", "true"],
        "timeout_seconds": 60,
        # The answer "no earlier dispatch", stated. An invocation that omits
        # it is refused by the schema rather than read as this.
        "prior_dispatch_at": "",
    }
    values.update(overrides)
    return SandboxJobRequest(**values)


async def _run_one(manager: JobManager, index: int, **overrides: Any) -> str:
    """Submit one job, let it finish, and return its id."""
    record = await manager.submit(_request(index, **overrides))
    if record.task is not None:
        await record.task
    return record.request.job_id


def _age(manager: JobManager, job_id: str, by: timedelta) -> None:
    """Move one job — and the ledger entry that names it — into the past.

    Everything retention is measured against moves together, because in the
    world being simulated they are one moment: a dispatch this runner accepted
    ``by`` ago, whose ledger entry is that old too.
    """
    record = manager._jobs[job_id]
    record.created_at -= by
    if record.finished_at is not None:
        record.finished_at -= by
    for invocation, dispatch in list(manager._invocations.items()):
        if dispatch.job_id == job_id:
            manager._invocations[invocation] = _Dispatch(job_id=job_id, at=dispatch.at - by)


class TestTheOutputOfOldJobsIsLetGo:
    async def test_only_the_most_recently_finished_jobs_keep_what_they_printed(self) -> None:
        manager = _manager(sandbox_job_output_retained_jobs=2)
        ids = [await _run_one(manager, index) for index in range(4)]
        # The release runs when the store grows, which is the only moment it
        # can: one more submit is what asks the question.
        await _run_one(manager, 99)

        kept = [manager._jobs[job_id] for job_id in ids[2:]]
        released = [manager._jobs[job_id] for job_id in ids[:2]]
        assert [record.output_retained for record in kept] == [True, True]
        assert all("output of" in record.stdout for record in kept)
        assert [record.output_retained for record in released] == [False, False]
        assert all(record.stdout == _OUTPUT_RELEASED_MARKER for record in released)

    async def test_a_released_stream_says_so_rather_than_reading_as_empty(self) -> None:
        """A job that printed nothing and a job whose words this process threw
        away are different facts. A caller told the first when the second is
        true reports a job that succeeded as one whose trailer could not be
        found, and has no way to learn why."""
        manager = _manager(sandbox_job_output_retained_jobs=0)
        first = await _run_one(manager, 1)
        await _run_one(manager, 2)

        response = manager._jobs[first].to_response()

        assert response.stdout == _OUTPUT_RELEASED_MARKER
        assert response.stdout_truncated is True
        # The answer itself is untouched: this is what a poller and the
        # reconciliation sweep actually ask a finished record for.
        assert (response.status, response.exit_code) == ("completed", 0)

    async def test_the_job_secrets_go_with_the_output(self) -> None:
        """A credential minted for one container has no business outliving the
        record of what that container printed."""
        manager = _manager(sandbox_job_output_retained_jobs=0)
        first = await _run_one(manager, 1, secret_env={"GIT_TOKEN": "ghs_token_value"})
        await _run_one(manager, 2)

        assert manager._jobs[first].request.secret_env == {}

    async def test_a_running_job_never_has_its_output_taken(self) -> None:
        manager = _manager(sandbox_job_output_retained_jobs=0)
        record = await manager.submit(_request(1))
        manager._release_surplus_output()

        assert record.output_retained is True
        if record.task is not None:
            await record.task


class TestARecordOutlivesTheSweepThatAsksAboutIt:
    """The interaction that makes this more than a TTL.

    ``jhin_tool_worker.sandbox_reconcile`` closes a row whose worker died by
    asking this runner about the job. It can ask as late as the row's own
    deadline — ``started_at`` plus the job's timeout — plus its grace and its
    interval, and a record dropped before then is one it would have to reason
    about without evidence.

    What it must *not* be is a promise the sweep then trusts blindly: a worker
    that is down longer than this window comes back and asks about jobs this
    runner finished and dropped, and no finite window covers a caller that is
    absent. So the window is published rather than assumed — see
    :class:`TestTheRunnerSaysWhatItsMemoryCovers` — and the sweep reads a 404
    against it instead of against a number of its own.
    """

    async def test_a_record_is_kept_while_that_sweep_can_still_reach_it(self) -> None:
        manager = _manager()
        job_id = await _run_one(manager, 1, timeout_seconds=1800)
        # Long past the retention window measured from its ending, and still
        # inside its own deadline plus that window.
        _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 60))

        manager._forget_expired(datetime.now(UTC))

        assert job_id in manager._jobs

    async def test_a_record_is_dropped_once_it_cannot(self) -> None:
        manager = _manager()
        job_id = await _run_one(manager, 1, timeout_seconds=1800)
        _age(manager, job_id, timedelta(seconds=1800 + _RETENTION_SECONDS + 60))

        manager._forget_expired(datetime.now(UTC))

        assert job_id not in manager._jobs

    async def test_a_job_that_has_not_finished_is_never_dropped(self) -> None:
        manager = _manager()
        record = await manager.submit(_request(1))
        record.created_at -= timedelta(days=7)

        manager._forget_expired(datetime.now(UTC))

        assert record.request.job_id in manager._jobs
        if record.task is not None:
            await record.task


class TestTheRunnerSaysWhatItsMemoryCovers:
    """Two facts, published because one caller cannot do its job without them.

    A 404 for a job means "I was not here when it ran" or "I ran it and have
    let the record go", and those want opposite treatment: the first is proof
    that nothing will ever report an outcome, the second is a job that may well
    have completed. The boundary between them is this process's to state.
    """

    async def test_the_window_it_publishes_is_the_window_it_enforces(self) -> None:
        """A copy of this number on the other side of the wire is a copy that
        goes stale the day somebody widens it here, so the sweep reads it —
        and it has to be the one actually applied."""
        manager = _manager()
        job_id = await _run_one(manager, 1, timeout_seconds=1800)
        published = manager.record_retention_seconds
        assert published == _RETENTION_SECONDS

        # Inside the published window, measured the way the sweep measures it:
        # the row's start plus its own timeout.
        _age(manager, job_id, timedelta(seconds=1800 + published - 60))
        manager._forget_expired(datetime.now(UTC))
        assert job_id in manager._jobs

        # And past it, which is the moment the sweep stops treating a 404 as
        # proof that nothing ran.
        _age(manager, job_id, timedelta(seconds=120))
        manager._forget_expired(datetime.now(UTC))
        assert job_id not in manager._jobs

    async def test_serving_since_is_the_moment_this_process_began_answering(self) -> None:
        """A job that started before it belongs to a previous incarnation,
        whose containers this one force-removed on the way up."""
        manager = _manager()

        assert manager.serving_since == manager._serving_since
        assert manager.serving_since < datetime.now(UTC)


class TestForgettingSaysHowFarBackTheMemoryReaches:
    """A bounded ledger has to be allowed to drop entries, and dropping them
    silently undoes the ledger: "I have no record of that invocation" would go
    back to meaning "so nothing ran", which is the inference that repeats an
    effect. Every forgotten dispatch moves the watermark instead."""

    async def test_the_ledger_entry_goes_with_the_record(self) -> None:
        manager = _manager()
        job_id = await _run_one(manager, 1, invocation_id=_INVOCATION)
        _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 3600))

        manager._forget_expired(datetime.now(UTC))

        assert manager._jobs == {}
        assert manager._invocations == {}

    async def test_a_dispatch_of_a_forgotten_invocation_is_refused_not_repeated(self) -> None:
        manager = _manager()
        job_id = await _run_one(manager, 1, invocation_id=_INVOCATION)
        first_dispatched_at = manager._invocations[_INVOCATION].at
        _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 3600))

        with pytest.raises(InvocationOutcomeUnknownError) as raised:
            await manager.submit(
                _request(
                    2,
                    invocation_id=_INVOCATION,
                    prior_dispatch_at=(
                        first_dispatched_at - timedelta(seconds=_RETENTION_SECONDS + 3600)
                    ).isoformat(),
                )
            )

        # Named as what it is. This runner was up the whole time; what it
        # cannot do is produce an outcome it has let go of.
        assert "oldest job this sandbox runner still holds" in str(raised.value)
        assert manager._jobs == {}

    async def test_a_dispatch_after_the_watermark_still_runs(self) -> None:
        """Forgetting one stretch of memory does not make the runner useless.
        Everything after the watermark is still covered, and the absence of a
        record there still proves nothing ran."""
        manager = _manager()
        job_id = await _run_one(manager, 1, invocation_id=_INVOCATION)
        _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 3600))
        manager._forget_expired(datetime.now(UTC))

        record = await manager.submit(
            _request(
                2,
                invocation_id="22222222-2222-7222-8222-222222222222",
                prior_dispatch_at=datetime.now(UTC).isoformat(),
            )
        )

        if record.task is not None:
            await record.task
        assert record.status == "completed"

    async def test_an_entry_naming_a_job_that_is_gone_refuses(self) -> None:
        """Unreachable while the two are dropped together, which is the
        invariant this asserts from the other side: if they ever come apart,
        the entry is proof that the invocation ran and that its outcome is
        gone — never a licence to run it again."""
        manager = _manager()
        job_id = await _run_one(manager, 1, invocation_id=_INVOCATION)
        del manager._jobs[job_id]

        with pytest.raises(InvocationOutcomeUnknownError) as raised:
            await manager.submit(_request(2, invocation_id=_INVOCATION))

        assert "no longer holds that job's outcome" in str(raised.value)

    async def test_a_restart_is_still_named_as_a_restart(self) -> None:
        """The other origin of the same silence, and an operator reading the
        refusal should not have to guess which one it was."""
        manager = _manager()
        manager._serving_since = datetime.now(UTC)

        with pytest.raises(InvocationOutcomeUnknownError) as raised:
            await manager.submit(
                _request(
                    1,
                    invocation_id=_INVOCATION,
                    prior_dispatch_at=(manager._serving_since - timedelta(minutes=5)).isoformat(),
                )
            )

        assert "before this sandbox runner" in str(raised.value)


class TestTheStoreStopsGrowing:
    async def test_a_run_of_jobs_leaves_neither_store_unbounded(self) -> None:
        """The measured shape: jobs arriving one after another, each holding
        its output and its ledger entry for the life of the process."""
        manager = _manager(sandbox_job_output_retained_jobs=4)
        for index in range(12):
            job_id = await _run_one(manager, index, invocation_id=f"{index:032x}")
            _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 3600))
        await _run_one(manager, 99)

        assert len(manager._jobs) == 1
        assert manager._invocations == {}
        assert manager._forgotten_through is not None

    async def test_a_dispatch_this_runner_is_about_to_forget_is_never_the_answer(self) -> None:
        """The sweep runs before the ledger is consulted, deliberately: a
        record on its way out must not be handed to a dispatch as an outcome,
        and its invocation must be reported as forgotten rather than as never
        seen."""
        manager = _manager()
        job_id = await _run_one(manager, 1, invocation_id=_INVOCATION)
        _age(manager, job_id, timedelta(seconds=_RETENTION_SECONDS + 3600))

        with pytest.raises(InvocationOutcomeUnknownError):
            await manager.submit(
                _request(
                    2,
                    invocation_id=_INVOCATION,
                    prior_dispatch_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                )
            )

    async def test_an_idle_runner_keeps_what_it_last_held(self) -> None:
        """Sweeping on submit is not a partial job: the store only grows
        there, so a runner that has stopped accepting work is already inside
        both bounds."""
        manager = _manager(sandbox_job_output_retained_jobs=1)
        first = await _run_one(manager, 1)
        second = await _run_one(manager, 2)
        await asyncio.sleep(0)

        assert manager._jobs[second].output_retained is True
        assert manager._jobs[first].output_retained is True
