"""Bounded, redacted snapshots become visible before a command exits."""

import asyncio
from typing import Any

import pytest

from jhin_sandbox_runner.jobs import JobManager, JobRecord
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings


def _record() -> JobRecord:
    request = SandboxJobRequest(job_id="abcdef1234", command=["echo", "safe"], secret_env={})
    return JobRecord(
        request=request, image="test", cpu_limit=1, memory_mb=256, pids_limit=64, timeout_seconds=30
    )


class StreamContainer:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def log(self, **kwargs: Any):
        assert kwargs["follow"] is True
        while (chunk := await self.queue.get()) is not None:
            yield chunk


@pytest.mark.asyncio
async def test_running_output_is_redacted_before_snapshot_and_bounded() -> None:
    manager = JobManager(
        Settings(
            sandbox_runner_token="test",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
            sandbox_max_output_bytes=100,
        )
    )
    record = _record()
    record.status = "running"
    record.redactor.register("secret-canary-token")
    manager._jobs[record.request.job_id] = record
    container = StreamContainer()
    capture = asyncio.create_task(manager._capture_stream(record, container, "stdout"))
    try:
        await container.queue.put("ready\nsecret-canary-")
        await asyncio.sleep(0)
        logs = await manager.current_logs(record.request.job_id)
        assert logs is not None and logs[0] == "ready\n[REDACTED]"
        await container.queue.put("token\ndone\n")
        await asyncio.sleep(0)
        assert "secret-canary" not in record.stdout
        assert "done" in record.stdout
        await container.queue.put("x" * 1_000 + "\nlast line\n")
        await asyncio.sleep(0)
        assert len(record.stdout.encode()) < 150
        assert record.stdout_truncated and "last line" in record.stdout
        await container.queue.put(None)
        await capture
    finally:
        capture.cancel()
        await asyncio.gather(capture, return_exceptions=True)


@pytest.mark.parametrize("fails", [False, True])
async def test_log_eof_or_failure_never_flushes_partial_secret(fails: bool) -> None:
    class InterruptedContainer:
        async def log(self, **kwargs: Any):
            yield "safe\nsecret-canary-"
            if fails:
                raise RuntimeError("logger disconnected")

    manager = JobManager(
        Settings(
            sandbox_runner_token="test",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
        )
    )
    record = _record()
    record.status = "running"
    record.redactor.register("secret-canary-token")
    await manager._capture_stream(record, InterruptedContainer(), "stderr")
    assert record.stderr == "safe\n[REDACTED]"
    assert record.stdout == ""
    assert record.status == "running" and record.exit_code is None
    assert record.stderr_truncated is fails


async def test_redaction_shrinkage_cannot_expose_a_clipped_secret_suffix() -> None:
    secret = "secret-canary-token"

    class RepeatedSecrets:
        async def log(self, **kwargs: Any):
            yield secret * 7

    manager = JobManager(
        Settings(
            sandbox_runner_token="test",
            sandbox_docker_mode="rootless",
            sandbox_docker_transport_url="http://rootless-docker-transport:2375",
            sandbox_max_output_bytes=100,
        )
    )
    record = _record()
    record.redactor.register(secret)
    await manager._capture_stream(record, RepeatedSecrets(), "stdout")
    assert record.stdout == "[REDACTED]" * 7
    assert record.stdout_truncated
