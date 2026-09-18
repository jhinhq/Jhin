"""One job at a time per workspace volume.

The disk an agent works on is shared, mutable state, and until now nothing
enforced that only one container was on it. The control plane's advisory lock
protects an *invocation*, which is exactly the wrong thing for the case that
matters: a tool worker killed mid-job leaves a container behind, not a
coroutine, and the tool call it abandoned is re-dispatched with a fresh
``job_id``. ``submit`` rejects only a duplicate id, so the guard could never
fire on a redispatch; two containers then ran on one disk, and for
``cli.repository.checkout`` — whose first act is to make the tree match the
remote — the second one deletes what the first is still writing.

The runner is the only process that can hold that line, because it is the one
that knows a container is still running. So it does: a job whose workspace is
held waits for it, and gives up having run nothing rather than joining it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jhin_sandbox_runner.jobs import JobManager
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

pytestmark = pytest.mark.anyio

WORKSPACE = "agent-0123456789abcdef-fedcba9876543210"
OTHER_WORKSPACE = "agent-0123456789abcdef-0000000000000001"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        sandbox_runner_token="test-token",
        sandbox_default_image="jhin-sandbox:test",
        sandbox_network="jhin_sandbox_test",
        sandbox_docker_mode="rootless",
        sandbox_docker_transport_url="http://rootless-docker-transport:2375",
        **overrides,
    )


class _Container:
    """A container that runs for as long as the test holds it.

    ``hold`` is the set of short job names whose containers stay ``Running``;
    anything else exits on its first poll, which is what an ordinary short job
    does and what keeps the tests that are not about waiting short.
    """

    def __init__(self, name: str, events: list[str], hold: set[str]) -> None:
        self.id = f"container-{name}"
        self._name = name
        self._events = events
        self._hold = hold
        self.exit_code = 0
        events.append(f"created {name}")

    @property
    def running(self) -> bool:
        return self._name in self._hold

    async def start(self) -> None:
        self._events.append(f"started {self._name}")

    async def show(self) -> dict[str, object]:
        return {"State": {"Running": self.running, "ExitCode": self.exit_code}}

    async def kill(self) -> None:
        self._hold.discard(self._name)

    async def log(self, **_kwargs: object):
        for item in ():
            yield item

    async def delete(self, **_kwargs: object) -> None:
        self._events.append(f"removed {self._name}")


class _Containers:
    def __init__(self, events: list[str], hold: set[str]) -> None:
        self.events = events
        self.hold = hold
        self.created: dict[str, _Container] = {}

    async def create(self, _config: dict[str, Any], *, name: str) -> _Container:
        short = name.removeprefix("jhin-sbx-")[:4]
        container = _Container(short, self.events, self.hold)
        self.created[short] = container
        return container

    def container(self, container_id: str) -> Any:
        for container in self.created.values():
            if container.id == container_id:
                return container
        raise KeyError(container_id)


class _Docker:
    def __init__(self, events: list[str], hold: set[str]) -> None:
        self.containers = _Containers(events, hold)

    async def close(self) -> None:
        pass


def _manager(
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    hold: set[str] | None = None,
    **overrides: Any,
) -> JobManager:
    manager = JobManager(_settings(**overrides))
    manager._docker = _Docker(events, hold if hold is not None else set())  # type: ignore[assignment]

    async def volume(self: JobManager, workspace_key: str, *, job_id: str) -> tuple[int, bool]:
        # The volume step runs an init container of its own *on the disk*, so
        # a gate that let this through would already have lost.
        events.append(f"volume {workspace_key} for {job_id[:4]}")
        return 0, False

    monkeypatch.setattr(JobManager, "_ensure_workspace_volume", volume)
    return manager


def _request(
    job_id: str, *, workspace_key: str = WORKSPACE, invocation_id: str = ""
) -> SandboxJobRequest:
    return SandboxJobRequest(
        job_id=job_id,
        image="jhin-sandbox:test",
        command=["bash", "-c", "true"],
        workspace_key=workspace_key,
        network_policy="none",
        timeout_seconds=300,
        invocation_id=invocation_id,
        # The answer "no earlier dispatch", stated. An invocation that omits
        # it is refused by the schema rather than read as this.
        prior_dispatch_at="",
    )


async def _settle(turns: int = 30) -> None:
    """Let every runnable task make progress, without deciding how long a
    thing that should not be happening would take to happen."""
    for _ in range(turns):
        await asyncio.sleep(0)
    await asyncio.sleep(0.15)


async def test_a_second_job_waits_for_the_first_to_let_go_of_the_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redispatch reproduction. Two jobs, two ids, one workspace."""
    events: list[str] = []
    hold = {"aaaa"}
    manager = _manager(events, monkeypatch, hold=hold)

    first = await manager.submit(_request("a" * 32))
    await _settle()
    assert first.status == "running"

    second = await manager.submit(_request("b" * 32))
    await _settle()

    # Nothing of the second job exists yet: no volume step, no container.
    assert second.status == "queued"
    assert second.container_id is None
    assert "created bbbb" not in events
    assert f"volume {WORKSPACE} for bbbb" not in events

    hold.discard("aaaa")
    await asyncio.wait_for(asyncio.gather(first.task, second.task), timeout=10)

    assert (first.status, second.status) == ("completed", "completed")
    # The whole invariant, in one line: the first container is gone before
    # the second one is made.
    assert events.index("removed aaaa") < events.index("created bbbb")
    assert events.index("removed aaaa") < events.index(f"volume {WORKSPACE} for bbbb")


async def test_a_job_on_a_different_workspace_is_not_held_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusion is per disk. Two agents are not each other's queue."""
    events: list[str] = []
    hold = {"aaaa", "bbbb"}
    manager = _manager(events, monkeypatch, hold=hold)

    first = await manager.submit(_request("a" * 32))
    second = await manager.submit(_request("b" * 32, workspace_key=OTHER_WORKSPACE))
    await _settle()

    assert (first.status, second.status) == ("running", "running")
    hold.clear()
    await asyncio.wait_for(asyncio.gather(first.task, second.task), timeout=10)
    assert (first.status, second.status) == ("completed", "completed")


async def test_a_job_with_no_workspace_is_not_gated_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    hold = {"aaaa", "bbbb"}
    manager = _manager(events, monkeypatch, hold=hold)

    first = await manager.submit(_request("a" * 32, workspace_key=""))
    second = await manager.submit(_request("b" * 32, workspace_key=""))
    await _settle()

    assert (first.status, second.status) == ("running", "running")
    hold.clear()
    await asyncio.wait_for(asyncio.gather(first.task, second.task), timeout=10)
    assert (first.status, second.status) == ("completed", "completed")


async def test_a_job_that_waits_too_long_fails_having_run_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue is bounded, because the caller polling this job has its own
    deadline and a container started long after it was asked for is work
    nobody is listening for. The refusal names the disk it waited on."""
    events: list[str] = []
    hold = {"aaaa"}
    manager = _manager(events, monkeypatch, hold=hold, sandbox_workspace_queue_seconds=0.2)

    first = await manager.submit(_request("a" * 32))
    await _settle()
    second = await manager.submit(_request("b" * 32))

    await asyncio.wait_for(second.task, timeout=10)

    assert second.status == "failed"
    assert second.container_id is None
    assert second.started_at is None
    assert WORKSPACE in (second.error or "")
    assert "never started" in (second.error or "")
    assert "created bbbb" not in events

    hold.discard("aaaa")
    await asyncio.wait_for(first.task, timeout=10)


async def test_a_queued_job_cancelled_while_waiting_ends_as_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that gives up, or a runner shutting down, must not leave a
    record with no ending — and must not start a container on the way out."""
    events: list[str] = []
    hold = {"aaaa"}
    manager = _manager(events, monkeypatch, hold=hold)

    first = await manager.submit(_request("a" * 32))
    await _settle()
    second = await manager.submit(_request("b" * 32))
    await _settle()
    assert second.status == "queued"

    await manager.cancel(second.request.job_id)
    await asyncio.wait_for(second.task, timeout=10)

    assert second.status == "cancelled"
    assert "created bbbb" not in events

    hold.discard("aaaa")
    await asyncio.wait_for(first.task, timeout=10)


async def test_the_disk_is_handed_on_even_when_the_first_job_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ending releases the workspace, or one broken job would wedge an
    agent's disk until the runner restarted."""
    events: list[str] = []
    manager = _manager(events, monkeypatch)

    async def explode(_config: dict[str, Any], *, name: str) -> Any:
        raise RuntimeError("the daemon said no")

    monkeypatch.setattr(manager._docker.containers, "create", explode)  # type: ignore[union-attr]
    first = await manager.submit(_request("a" * 32))
    await asyncio.wait_for(first.task, timeout=10)
    assert first.status == "failed"
    assert manager._workspace_holders == {}


class TestWhatAQueuedOutDispatchIsWorthToTheLedger:
    """A dispatch that ran nothing is not that invocation's answer.

    The invocation ledger and this queue meet here, and the meeting used to go
    badly. ``submit`` writes the ledger entry before the job has done
    anything — which is what makes the claim atomic, and correct while the job
    is queued: a second dispatch arriving then must join the first, not start
    beside it. But a job that gives up in this queue has, by the queue's own
    construction, started no init container, taken no measurement and run no
    container; keeping it as the invocation's answer meant every later
    dispatch of that tool call was replayed onto a failure and the call was
    never run at all. Not idempotency — a lost call.

    So the entry is taken back as such a job ends, and only for that ending:
    past the queue the runner cannot prove nothing happened.
    """

    INVOCATION = "11111111-1111-7111-8111-111111111111"

    async def test_a_dispatch_that_gave_up_is_not_replayed_onto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        hold = {"aaaa"}
        manager = _manager(events, monkeypatch, hold=hold, sandbox_workspace_queue_seconds=0.2)

        holder = await manager.submit(_request("a" * 32))
        await _settle()
        queued_out = await manager.submit(_request("b" * 32, invocation_id=self.INVOCATION))
        await asyncio.wait_for(queued_out.task, timeout=10)
        assert queued_out.status == "failed"
        assert "created bbbb" not in events

        hold.discard("aaaa")
        await asyncio.wait_for(holder.task, timeout=10)
        retried = await manager.submit(_request("c" * 32, invocation_id=self.INVOCATION))
        await asyncio.wait_for(retried.task, timeout=10)

        # The whole point: this dispatch ran, rather than being handed a
        # record of a job that never touched the disk.
        assert retried is not queued_out
        assert retried.status == "completed"
        assert "created cccc" in events

    async def test_a_dispatch_still_waiting_is_still_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim is taken back only when the wait is over. While a
        dispatch is queued it may yet run, and a second dispatch of it must
        join that one rather than start a container beside it."""
        events: list[str] = []
        hold = {"aaaa"}
        manager = _manager(events, monkeypatch, hold=hold)

        holder = await manager.submit(_request("a" * 32))
        await _settle()
        queued = await manager.submit(_request("b" * 32, invocation_id=self.INVOCATION))
        await _settle()
        assert queued.status == "queued"

        second = await manager.submit(_request("c" * 32, invocation_id=self.INVOCATION))

        assert second is queued
        assert "created cccc" not in events
        hold.discard("aaaa")
        await asyncio.wait_for(asyncio.gather(holder.task, queued.task), timeout=10)

    async def test_a_dispatch_cancelled_while_queued_is_not_replayed_onto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same ending by another name. What decides is that nothing ran, not
        why the waiting stopped."""
        events: list[str] = []
        hold = {"aaaa"}
        manager = _manager(events, monkeypatch, hold=hold)

        holder = await manager.submit(_request("a" * 32))
        await _settle()
        cancelled = await manager.submit(_request("b" * 32, invocation_id=self.INVOCATION))
        await _settle()
        await manager.cancel(cancelled.request.job_id)
        await asyncio.wait_for(cancelled.task, timeout=10)
        assert cancelled.status == "cancelled"

        hold.discard("aaaa")
        await asyncio.wait_for(holder.task, timeout=10)
        retried = await manager.submit(_request("c" * 32, invocation_id=self.INVOCATION))
        await asyncio.wait_for(retried.task, timeout=10)

        assert retried is not cancelled
        assert retried.status == "completed"

    async def test_a_dispatch_that_ran_is_kept_however_it_ended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The line is "nothing ran", not "it failed". A job whose container
        was made and whose ending was a failure is still the only account of
        what that container did, and a second dispatch gets it rather than a
        second container."""
        events: list[str] = []
        manager = _manager(events, monkeypatch)

        async def explode(_config: dict[str, Any], *, name: str) -> Any:
            raise RuntimeError("the daemon said no")

        monkeypatch.setattr(manager._docker.containers, "create", explode)  # type: ignore[union-attr]
        first = await manager.submit(_request("a" * 32, invocation_id=self.INVOCATION))
        await asyncio.wait_for(first.task, timeout=10)
        assert first.status == "failed"

        second = await manager.submit(_request("b" * 32, invocation_id=self.INVOCATION))

        assert second is first
