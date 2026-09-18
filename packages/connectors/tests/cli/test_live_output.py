"""Runner snapshots are best-effort observations, never another dispatch."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from jhin_connectors.cli import runner_client
from jhin_connectors.cli import tools as cli_tools
from jhin_db.models import SandboxJob
from jhin_domain import new_uuid7
from jhin_sandbox_runner.jobs import JobManager
from jhin_secrets import get_redactor
from jhin_secrets.redaction import SecretRedactor


@pytest.mark.parametrize("observer_fails", [False, True])
async def test_progress_polls_attached_job_and_never_changes_dispatch(
    monkeypatch: pytest.MonkeyPatch, observer_fails: bool
) -> None:
    requests: list[str] = []
    snapshots: list[dict[str, Any]] = []
    polls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "attached"})
        polls += 1
        return httpx.Response(
            200,
            json={
                "job_id": "attached",
                "status": "running" if polls == 1 else "completed",
                "stdout": "first line\n" if polls == 1 else "first line\nlast line\n",
                "stderr": "",
                "exit_code": None if polls == 1 else 0,
            },
        )

    async def observe(snapshot: dict[str, Any]) -> None:
        snapshots.append(snapshot)
        if observer_fails:
            raise RuntimeError("progress DB unavailable")

    client_type = httpx.AsyncClient
    monkeypatch.setenv("SANDBOX_RUNNER_TOKEN", "test-token")
    monkeypatch.setattr(runner_client, "_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        runner_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    with runner_client.sandbox_job_progress(observe):
        result = await runner_client.run_sandbox_job({"job_id": "new-job"}, job_timeout_seconds=30)
    assert result["status"] == "completed" and result["polled_job_id"] == "attached"
    assert snapshots[0]["stdout"] == "first line\n"
    assert requests == ["POST /v1/jobs", "GET /v1/jobs/attached", "GET /v1/jobs/attached"]


async def test_progress_is_durable_redacted_bounded_and_cannot_reopen_terminal_job(
    session, context
) -> None:
    await session.commit()
    sessions = async_sessionmaker(session.bind, expire_on_commit=False)
    ctx = replace(context, session_factory=sessions, tool_call_id=new_uuid7())
    row = SandboxJob(
        id=new_uuid7(),
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        run_id=ctx.run_id,
        tool_call_id=ctx.tool_call_id,
        status="running",
        image="test",
        network_policy="none",
    )
    evidence = await cli_tools._JobEvidence.opened(ctx, row, network="none", shared={}, metadata={})
    redactor = get_redactor()
    redactor.register("worker-only-canary-secret")
    try:
        await evidence.progress(
            {"status": "running", "stdout": "first\nworker-only-canary-", "stderr": "\x00oops"}
        )
        async with sessions() as reader:
            saved = await reader.get(SandboxJob, row.id)
            assert saved is not None and saved.stdout_tail == "first\n[REDACTED]"
            assert saved.stderr_tail == "?oops" and saved.status == "running"
        await evidence.progress(
            {"status": "running", "stdout": "x" * 20_000 + "last line", "stderr": ""}
        )
        async with sessions() as reader:
            saved = await reader.get(SandboxJob, row.id)
            assert saved is not None and len(saved.stdout_tail) <= 8_192
            assert saved.stdout_tail.endswith("last line")
            await reader.execute(
                update(SandboxJob)
                .where(SandboxJob.id == row.id)
                .values(status="completed", stdout_tail="final")
            )
            await reader.commit()
        await evidence.progress({"status": "running", "stdout": "stale progress", "stderr": ""})
        async with sessions() as reader:
            saved = await reader.scalar(select(SandboxJob).where(SandboxJob.id == row.id))
            assert (
                saved is not None and saved.status == "completed" and saved.stdout_tail == "final"
            )
    finally:
        redactor.clear()


async def test_cancelled_observer_propagates_without_cancelling_or_resubmitting_job(monkeypatch):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(
            202 if request.method == "POST" else 200,
            json={"job_id": "attached", "status": "running"},
        )

    async def cancelled(snapshot):
        raise asyncio.CancelledError

    client_type = httpx.AsyncClient
    monkeypatch.setenv("SANDBOX_RUNNER_TOKEN", "test-token")
    monkeypatch.setattr(
        runner_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )
    with runner_client.sandbox_job_progress(cancelled), pytest.raises(asyncio.CancelledError):
        await runner_client.run_sandbox_job({"job_id": "new"}, job_timeout_seconds=30)
    assert requests == [("POST", "/v1/jobs"), ("GET", "/v1/jobs/attached")]


async def test_runner_truncation_notice_preserves_secret_boundary_and_progress_notice(
    session, context
):
    redactor = get_redactor()
    redactor.register("worker-only-canary-secret")
    notice = "\n…[truncated by sandbox runner]"
    snapshot, truncated = JobManager._sanitize(
        SimpleNamespace(redactor=SecretRedactor()), "界" * 100 + "worker-only-canary-", 100
    )
    assert truncated and snapshot.endswith(notice)
    try:
        safe = cli_tools._tail(snapshot)
        assert "worker-only-canary-" not in safe and "[REDACTED]" in safe
        assert safe.endswith(notice)
        await session.commit()
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        ctx = replace(context, session_factory=sessions, tool_call_id=new_uuid7())
        row = SandboxJob(
            id=new_uuid7(),
            workspace_id=ctx.workspace_id,
            run_id=ctx.run_id,
            task_id=ctx.task_id,
            tool_call_id=ctx.tool_call_id,
            status="running",
            image="test",
            network_policy="none",
        )
        evidence = await cli_tools._JobEvidence.opened(
            ctx, row, network="none", shared={}, metadata={}
        )
        await evidence.progress({"status": "running", "stdout": snapshot, "stderr": snapshot})
        async with sessions() as reader:
            saved = await reader.get(SandboxJob, row.id)
            assert saved is not None and saved.stdout_tail == safe and saved.stderr_tail == safe
        row.status = "completed"
        row.stdout_tail = cli_tools._tail(snapshot)
        row.stderr_tail = cli_tools._tail(snapshot)
        await evidence.closed("sandbox.job.completed", {})
        async with sessions() as reader:
            saved = await reader.get(SandboxJob, row.id)
            assert saved is not None and saved.status == "completed" and saved.stdout_tail == safe
    finally:
        redactor.clear()
