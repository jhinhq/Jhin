"""Job lifecycle publication tests for the sandbox runner."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from jhin_sandbox_runner.jobs import JobManager, JobRecord
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings


class _BlockingDeleteContainer:
    id = "sandbox-container-id"

    def __init__(self) -> None:
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()
        self.killed = False

    async def start(self) -> None:
        return None

    async def show(self) -> dict[str, dict[str, int | bool]]:
        return {"State": {"Running": not self.killed, "ExitCode": 137}}

    async def kill(self) -> None:
        self.killed = True

    async def log(self, **_kwargs: bool) -> list[str]:
        return []

    async def delete(self, *, force: bool, v: bool) -> None:
        self.delete_started.set()
        await self.allow_delete.wait()


class _FakeContainers:
    def __init__(self, container: _BlockingDeleteContainer) -> None:
        self._container = container

    async def create(self, _config: dict[str, Any], *, name: str) -> _BlockingDeleteContainer:
        return self._container


class _FakeDocker:
    def __init__(self, container: _BlockingDeleteContainer) -> None:
        self.containers = _FakeContainers(container)


@pytest.mark.asyncio
async def test_terminal_status_waits_for_container_deletion() -> None:
    """A terminal API state guarantees its ephemeral container is gone."""
    request = SandboxJobRequest(
        job_id="0123456789abcdef",
        command=["bash", "-c", "sleep 120"],
        timeout_seconds=1,
    )
    record = JobRecord(
        request=request,
        image="jhin-sandbox:test",
        cpu_limit=1.0,
        memory_mb=128,
        pids_limit=16,
        timeout_seconds=0,
    )
    container = _BlockingDeleteContainer()
    manager = JobManager(Settings(sandbox_runner_token="test-token"))
    manager._docker = cast(Any, _FakeDocker(container))

    run_task = asyncio.create_task(manager._run(record))
    try:
        await asyncio.wait_for(container.delete_started.wait(), timeout=1.0)
        assert record.status == "running"
        assert record.finished_at is None
    finally:
        container.allow_delete.set()
        await run_task

    assert record.status == "timeout"
    assert record.finished_at is not None
