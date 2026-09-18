"""What the runner does with jobs that are still going when it is told to stop.

Shutdown used to cancel the ``_run`` tasks and close the Docker client in the
same breath, which is two problems in one line. A cancelled ``_run`` reaches
its ``finally`` on a later turn of the loop, by which time the client it needs
to remove the container has gone; and a cancelled job never becomes anything
the caller can read — it just stops, and the tool worker polling it gets no
ending at all.

Draining uses the ordinary cancel path instead: set ``cancel_requested``, kill
the container, and let each job finish becoming ``cancelled`` the way a
caller's ``POST /cancel`` would. The tool worker then reads a terminal status
and closes its own ``sandbox_job`` row, which is the difference between a
readable failure and an orphan.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jhin_sandbox_runner.jobs import JobManager, JobRecord
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

SETTINGS = Settings(
    sandbox_runner_token="test-token",
    sandbox_default_image="jhin-sandbox:test",
    sandbox_network="jhin_sandbox_test",
    sandbox_docker_mode="rootless",
    sandbox_docker_transport_url="http://rootless-docker-transport:2375",
    sandbox_drain_timeout_seconds=1.0,
)


class _Container:
    def __init__(self, killed: list[str], container_id: str) -> None:
        self._killed = killed
        self.id = container_id

    async def kill(self) -> None:
        self._killed.append(self.id)


class _Containers:
    def __init__(self, killed: list[str]) -> None:
        self._killed = killed

    def container(self, container_id: str) -> _Container:
        return _Container(self._killed, container_id)


class _Docker:
    """Just enough aiodocker for ``close()``: containers to kill, and a client
    that records whether it was shut before the jobs were."""

    def __init__(self, killed: list[str], order: list[str]) -> None:
        self.containers = _Containers(killed)
        self._order = order

    async def close(self) -> None:
        self._order.append("docker closed")


def _request(job_id: str) -> SandboxJobRequest:
    return SandboxJobRequest(
        job_id=job_id,
        image="jhin-sandbox:test",
        command=["bash", "-c", "sleep 600"],
        network_policy="none",
        timeout_seconds=300,
    )


def _manager(killed: list[str], order: list[str]) -> JobManager:
    manager = JobManager(SETTINGS)
    manager._docker = _Docker(killed, order)  # type: ignore[assignment]
    return manager


def _record(job_id: str, container_id: str) -> JobRecord:
    return JobRecord(
        request=_request(job_id),
        image="jhin-sandbox:test",
        cpu_limit=1.0,
        memory_mb=512,
        pids_limit=64,
        timeout_seconds=300,
        container_id=container_id,
    )


async def test_a_drained_job_is_asked_to_stop_and_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[str] = []
    order: list[str] = []
    manager = _manager(killed, order)
    record = _record("0" * 32, "container-1")

    async def run() -> None:
        # Stands in for ``_run``: it watches ``cancel_requested`` the way
        # ``_wait`` does, and ends by writing a terminal status the way the
        # ``finally`` does.
        # Polled, not awaited on an event, because that is what ``_wait``
        # does with ``cancel_requested`` — a stand-in that waited on something
        # the manager does not set would prove nothing about the drain.
        while not record.cancel_requested:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        record.status = "cancelled"
        order.append("job finished")

    record.task = asyncio.create_task(run())
    manager._jobs[record.request.job_id] = record

    await manager.close()

    assert record.status == "cancelled"
    assert killed == ["container-1"]
    # Ordering is the point: the client that removes containers is still
    # there while the jobs are ending.
    assert order == ["job finished", "docker closed"]


async def test_a_job_that_will_not_end_costs_only_the_drain_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget belongs to Docker's stop grace, not to us. A job that
    ignores the kill is abandoned to the old cancellation, and the runner's
    startup reaping removes whatever container it leaves."""
    killed: list[str] = []
    order: list[str] = []
    manager = _manager(killed, order)
    record = _record("1" * 32, "container-2")

    async def never_ends() -> None:
        await asyncio.sleep(600)

    record.task = asyncio.create_task(never_ends())
    manager._jobs[record.request.job_id] = record

    loop = asyncio.get_running_loop()
    started = loop.time()
    await manager.close()
    elapsed = loop.time() - started

    assert elapsed < 5.0
    assert record.task.cancelled() or record.task.cancelling()
    assert order == ["docker closed"]


async def test_a_runner_with_nothing_in_flight_just_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[str] = []
    order: list[str] = []
    manager = _manager(killed, order)

    await manager.close()

    assert (killed, order) == ([], ["docker closed"])


async def test_a_container_that_refuses_the_kill_does_not_stop_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker can answer anything while it is being torn down too; the drain
    is best effort about the kill and firm about the wait."""
    order: list[str] = []

    class _Angry(_Containers):
        def container(self, container_id: str) -> Any:
            raise RuntimeError("docker is going away")

    manager = _manager([], order)
    manager._docker.containers = _Angry([])  # type: ignore[union-attr]
    record = _record("2" * 32, "container-3")

    async def run() -> None:
        # Polled, not awaited on an event, because that is what ``_wait``
        # does with ``cancel_requested`` — a stand-in that waited on something
        # the manager does not set would prove nothing about the drain.
        while not record.cancel_requested:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        record.status = "cancelled"
        order.append("job finished")

    record.task = asyncio.create_task(run())
    manager._jobs[record.request.job_id] = record

    await manager.close()

    assert record.status == "cancelled"
    assert order == ["job finished", "docker closed"]
