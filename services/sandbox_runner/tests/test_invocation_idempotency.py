"""One dispatch per invocation, decided where both dispatches are visible.

A tool call whose worker dies is re-dispatched with a fresh ``job_id``, so
nothing on the wire relates the two attempts. Every attempt to settle that
inside the job — by looking at what the workspace holds afterwards — failed,
and had to: a file records an *effect*, and "did my earlier dispatch run" is a
question about an *event*. This process is the only one that watches both
attempts, so these tests are about the thing it can say that nothing else can.

The adversary's shape is the last test here, and it is the one that broke
every content-based guard: a file containing ``call(alpha, alpha, beta)``,
``old_string`` ``alpha, beta``, ``new_string`` ``beta, alpha``. Applying it
once leaves ``alpha, beta`` occurring exactly once again, so the occurrence
count agrees a second time; and the result contains ``beta, alpha``, so a
"has this already landed" guard agrees too. Both guards pass and the file
changes on both dispatches. Nothing about the file can stop that. Recognising
the invocation can, and does.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from jhin_sandbox_runner.jobs import (
    InvocationOutcomeUnknownError,
    JobManager,
    JobValidationError,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

_INVOCATION = "11111111-1111-7111-8111-111111111111"


def runner_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sandbox_runner_token": "test-token",
        "sandbox_docker_mode": "rootless",
        "sandbox_docker_transport_url": "http://rootless-docker-transport:2375",
    }
    values.update(overrides)
    return Settings(**values)


class _Container:
    """A container that exits as soon as it is looked at.

    Its whole job is to be countable. A test here fails by there being two of
    these, not by anything one of them did.
    """

    def __init__(self, name: str) -> None:
        self.id = f"container-{name}"
        self.name = name
        self.running = False
        self.killed = False
        self.deleted = False

    async def start(self) -> None:
        return None

    async def show(self) -> dict[str, dict[str, Any]]:
        return {"State": {"Running": self.running, "ExitCode": 0}}

    async def kill(self) -> None:
        self.killed = True
        self.running = False

    async def log(self, **_kwargs: bool):
        yield f"ran {self.name}\n"

    async def delete(self, *, force: bool, v: bool) -> None:
        del force, v
        self.deleted = True


class _Containers:
    def __init__(self, owner: _Docker) -> None:
        self._owner = owner

    async def create(self, _config: dict[str, Any], *, name: str) -> _Container:
        container = _Container(name)
        self._owner.created.append(container)
        return container

    def container(self, container_id: str) -> _Container:
        for made in self._owner.created:
            if made.id == container_id:
                return made
        raise AssertionError(f"no such container: {container_id}")


class _Docker:
    """Records every container this runner asked Docker to make.

    The count is the whole assertion in most of these tests: an invocation
    that ran twice is an invocation that made two containers, whatever the
    status documents say afterwards.
    """

    def __init__(self) -> None:
        self.created: list[_Container] = []
        self.containers = _Containers(self)


def _request(job_id: str, **overrides: object) -> SandboxJobRequest:
    values: dict[str, Any] = {
        "job_id": job_id,
        "command": ["bash", "-c", "true"],
        "timeout_seconds": 60,
        # Stated, the way the real caller states it. The empty string is the
        # answer "there was no earlier dispatch", and an invocation that
        # leaves the question unanswered is refused rather than read as that
        # answer — see ``TestTheStampIsAnAnswerAndNotADefault``.
        "prior_dispatch_at": "",
    }
    values.update(overrides)
    return SandboxJobRequest(**values)


async def _manager() -> tuple[JobManager, _Docker]:
    docker = _Docker()
    manager = JobManager(runner_settings())
    manager._docker = cast(Any, docker)
    return manager, docker


async def _finish(manager: JobManager, docker: _Docker) -> None:
    """Wait for every job this runner accepted to reach its ending."""
    del docker
    tasks = [record.task for record in manager._jobs.values() if record.task is not None]
    if tasks:
        await asyncio.gather(*tasks)


class TestASecondDispatchOfOneInvocationRunsNothing:
    async def test_it_is_handed_the_first_dispatch_s_job(self) -> None:
        manager, docker = await _manager()
        first = await manager.submit(_request("aaaaaaaa1111", invocation_id=_INVOCATION))
        second = await manager.submit(_request("bbbbbbbb2222", invocation_id=_INVOCATION))

        assert second is first
        assert second.request.job_id == "aaaaaaaa1111"
        # The second dispatch's own id was never registered at all: there is
        # one job here, not two with one of them idle.
        assert set(manager._jobs) == {"aaaaaaaa1111"}
        await _finish(manager, docker)
        assert len(docker.created) == 1

    async def test_the_outcome_of_a_finished_first_dispatch_is_returned(self) -> None:
        """The case the re-dispatch machinery exists for: the first attempt
        finished, and the worker that was watching it died before it could
        write down what happened."""
        manager, docker = await _manager()
        await manager.submit(_request("aaaaaaaa1111", invocation_id=_INVOCATION))
        await _finish(manager, docker)

        replayed = await manager.submit(_request("cccccccc3333", invocation_id=_INVOCATION))

        assert replayed.status == "completed"
        assert "ran " in replayed.stdout
        # Still one container. The second dispatch read an outcome; it did not
        # produce one.
        assert len(docker.created) == 1

    async def test_a_different_invocation_is_a_different_job(self) -> None:
        manager, docker = await _manager()
        first = await manager.submit(_request("aaaaaaaa1111", invocation_id=_INVOCATION))
        other = await manager.submit(
            _request("bbbbbbbb2222", invocation_id="22222222-2222-7222-8222-222222222222")
        )

        assert other is not first
        await _finish(manager, docker)
        assert len(docker.created) == 2

    async def test_a_job_with_no_invocation_identity_gets_no_idempotency(self) -> None:
        """Honest default. A caller with nothing stable to name is told
        nothing about repeats, rather than having two unrelated jobs collapsed
        into one because they both offered an empty string."""
        manager, docker = await _manager()
        first = await manager.submit(_request("aaaaaaaa1111"))
        second = await manager.submit(_request("bbbbbbbb2222"))

        assert second is not first
        await _finish(manager, docker)
        assert len(docker.created) == 2


class TestAnInvocationThisRunnerCannotAccountFor:
    """What happens when the runner itself died between the two dispatches.

    Its ledger died with it, and its restart force-removed the first
    dispatch's container part-way through whatever it was doing. So the
    honest answer is not "run it again" and not "it succeeded" — it is that
    the first attempt's outcome cannot be established here.

    The runner tells the two cases apart with one comparison, and it is exact
    rather than heuristic: a dispatch stamped *before* this process began
    serving is one this process could not have seen, while a dispatch stamped
    *after* is one it would be holding a record of if it had ever received it
    — so the absence of that record proves the job was never submitted and
    nothing ran.
    """

    async def test_a_dispatch_from_before_this_runner_started_is_refused(self) -> None:
        manager, docker = await _manager()
        manager._serving_since = datetime.now(UTC)
        earlier = (manager._serving_since - timedelta(minutes=5)).isoformat()

        with pytest.raises(InvocationOutcomeUnknownError):
            await manager.submit(
                _request(
                    "aaaaaaaa1111",
                    invocation_id=_INVOCATION,
                    prior_dispatch_at=earlier,
                )
            )

        # Nothing started, and nothing was recorded as having started.
        assert manager._jobs == {}
        assert docker.created == []

    async def test_a_dispatch_from_after_this_runner_started_runs(self) -> None:
        """This runner was up, and never saw it. That is proof the earlier
        dispatch never reached a container — its worker died between writing
        the row and submitting — so running it now is not a repeat."""
        manager, docker = await _manager()
        manager._serving_since = datetime.now(UTC) - timedelta(minutes=5)
        later = (manager._serving_since + timedelta(minutes=1)).isoformat()

        record = await manager.submit(
            _request("aaaaaaaa1111", invocation_id=_INVOCATION, prior_dispatch_at=later)
        )

        assert record.request.job_id == "aaaaaaaa1111"
        await _finish(manager, docker)
        assert len(docker.created) == 1

    async def test_a_known_invocation_is_replayed_whatever_the_stamp_says(self) -> None:
        """Memory beats inference. The comparison is only ever consulted when
        there is nothing to remember."""
        manager, docker = await _manager()
        first = await manager.submit(_request("aaaaaaaa1111", invocation_id=_INVOCATION))
        ancient = (manager._serving_since - timedelta(days=1)).isoformat()

        second = await manager.submit(
            _request("bbbbbbbb2222", invocation_id=_INVOCATION, prior_dispatch_at=ancient)
        )

        assert second is first
        await _finish(manager, docker)
        assert len(docker.created) == 1

    async def test_the_margin_errs_towards_refusing(self) -> None:
        """A dispatch stamped a moment after this process opened its socket
        may have been stamped a moment before it. The two mistakes are not the
        same size — one costs a tool call that ran nothing and said so, the
        other costs an edit applied twice — so the margin refuses."""
        manager, docker = await _manager()
        manager._serving_since = datetime.now(UTC)
        just_after = (manager._serving_since + timedelta(seconds=1)).isoformat()

        with pytest.raises(InvocationOutcomeUnknownError):
            await manager.submit(
                _request(
                    "aaaaaaaa1111",
                    invocation_id=_INVOCATION,
                    prior_dispatch_at=just_after,
                )
            )
        assert docker.created == []


class TestTheStampIsAnAnswerAndNotADefault:
    """A second client of ``POST /v1/jobs`` cannot break this by saying
    nothing.

    ``prior_dispatch_at`` decides whether an invocation this runner has no
    record of gets a container, and the value that means "there was no earlier
    dispatch" is the empty string — which is also what a client that has never
    heard of the field sends. Shape validation cannot tell those apart. Today
    exactly one caller exists and it always answers; the way that stops being
    load-bearing is that an invocation which leaves the question unanswered is
    refused.
    """

    def test_an_invocation_that_omits_the_stamp_is_refused_by_the_schema(self) -> None:
        with pytest.raises(ValueError, match="prior_dispatch_at is required"):
            SandboxJobRequest(
                job_id="aaaaaaaa1111",
                command=["bash", "-c", "true"],
                invocation_id=_INVOCATION,
            )

    def test_a_caller_that_offers_no_invocation_is_untouched(self) -> None:
        """The pairing an upgrade depends on: an older tool worker sends
        neither field, gets no ledger entry, and this one is never read for
        it."""
        request = SandboxJobRequest(job_id="aaaaaaaa1111", command=["bash", "-c", "true"])

        assert request.invocation_id == ""
        assert request.prior_dispatch_moment() is None

    async def test_the_manager_refuses_it_too_rather_than_trusting_validation(self) -> None:
        """``submit`` is reachable in process, not only through the route, so
        the interlock does not rest on the schema having run."""
        manager, docker = await _manager()
        unstated = SandboxJobRequest.model_construct(
            job_id="aaaaaaaa1111",
            command=["bash", "-c", "true"],
            timeout_seconds=60,
            invocation_id=_INVOCATION,
        )

        with pytest.raises(JobValidationError, match="must state prior_dispatch_at"):
            await manager.submit(unstated)

        assert manager._jobs == {}
        assert docker.created == []


class TestWhenTheTwoClocksDisagree:
    """The one input that turns a refusal into a second container.

    The comparison that decides whether an earlier dispatch is one this runner
    could have seen is made between two clocks, and the shipped topology gives
    it one clock: both processes are containers on a single Docker host. Split
    them across hosts without synchronising and a caller running ahead stamps
    dispatches that look newer than this runner's memory — which is the
    direction that runs a job twice. Half of that is detectable from here, and
    it is the dangerous half.
    """

    async def test_a_stamp_in_this_runners_future_is_refused_and_names_the_clocks(self) -> None:
        manager, docker = await _manager()
        manager._serving_since = datetime.now(UTC) - timedelta(minutes=5)
        ahead = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()

        with pytest.raises(InvocationOutcomeUnknownError) as raised:
            await manager.submit(
                _request("aaaaaaaa1111", invocation_id=_INVOCATION, prior_dispatch_at=ahead)
            )

        # An operator who reads this goes and looks at the clocks, which is a
        # different action from the other refusal's "nothing to do".
        assert "clock" in str(raised.value)
        assert manager._jobs == {}
        assert docker.created == []

    async def test_the_ordering_margin_is_still_allowed_for(self) -> None:
        """The caller stamps its row and *then* submits, so a stamp a moment
        ahead of this runner is ordinary and must not be mistaken for a broken
        deployment."""
        manager, docker = await _manager()
        manager._serving_since = datetime.now(UTC) - timedelta(minutes=5)
        barely_ahead = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()

        record = await manager.submit(
            _request("aaaaaaaa1111", invocation_id=_INVOCATION, prior_dispatch_at=barely_ahead)
        )

        assert record.request.job_id == "aaaaaaaa1111"
        await _finish(manager, docker)
        assert len(docker.created) == 1


class TestTheAdversarysEdit:
    """The shape no guard over file contents can catch, caught.

    ``call(alpha, alpha, beta)`` with old ``alpha, beta`` and new
    ``beta, alpha``: one application gives ``call(alpha, beta, alpha)``, a
    second gives ``call(beta, alpha, alpha)``, and the occurrence count is one
    both times. This test does not run the edit — it runs the boundary, which
    is where the repeat is stopped, and asserts the only thing that matters:
    the second dispatch produces no container to apply anything with.
    """

    async def test_the_second_dispatch_produces_no_container(self) -> None:
        manager, docker = await _manager()
        edit = ["bash", "-c", "python3 - <<'PY'\n# the edit program\nPY"]
        first = await manager.submit(
            _request("aaaaaaaa1111", command=edit, invocation_id=_INVOCATION)
        )
        await _finish(manager, docker)

        second = await manager.submit(
            _request("bbbbbbbb2222", command=edit, invocation_id=_INVOCATION)
        )

        assert second is first
        assert len(docker.created) == 1
        assert second.status == "completed"


class TestWhatTheCallerIsToldAboutBudgets:
    async def test_the_pre_start_budget_is_the_queue_plus_the_measurement(self) -> None:
        """The caller's poll deadline has to cover what this runner spends
        before the container exists, and the only way it can is by being told.
        A copy of these numbers kept on the other side of the wire is a copy
        that goes stale the day one of them is widened — which is exactly how
        a sixty-second measurement came to be paid for out of a sixty-second
        grace."""
        manager, _ = await _manager()
        record = await manager.submit(_request("aaaaaaaa1111"))

        response = record.to_response()
        # 30s queue + the 5s the initializer gets to start + 60s of walking.
        assert response.pre_start_budget_seconds == 95
        assert response.invocation_id == ""

    async def test_the_budget_follows_the_settings(self) -> None:
        docker = _Docker()
        manager = JobManager(
            runner_settings(
                sandbox_workspace_queue_seconds=2.5,
                sandbox_workspace_measure_budget_seconds=7,
            )
        )
        manager._docker = cast(Any, docker)
        record = await manager.submit(_request("aaaaaaaa1111"))

        # Rounded up, because a partial second of waiting is still a second
        # the caller must not spend on the job's own clock: 3 + 5 + 7.
        assert record.to_response().pre_start_budget_seconds == 15

    async def test_the_invocation_is_echoed_back(self) -> None:
        manager, _ = await _manager()
        record = await manager.submit(_request("aaaaaaaa1111", invocation_id=_INVOCATION))

        assert record.to_response().invocation_id == _INVOCATION
