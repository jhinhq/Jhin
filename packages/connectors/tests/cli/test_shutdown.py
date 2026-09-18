"""What happens to a sandbox job when the worker running it goes away.

The container is left running, and that is the point of these tests.

Killing it looked like the tidy thing: close the row, stop the process, leave
nothing behind. It is the wrong thing twice over. The container is the only
thing that still knows what this call did, and the re-dispatch that follows a
redeploy asks the runner for exactly that — let it finish and the second
dispatch is handed a real outcome, kill it and the second dispatch inherits a
workspace stopped somewhere nobody can name. And a kill lands wherever it
lands: ``cli.file.write`` pushes up to 48,000 characters through stdio, so a
kill between two flushes used to leave a truncated file. (That second hazard
is closed on its own terms as well — both writing tools stage and rename, see
``test_redispatch_guards.py`` — because a container can still be killed by its
own timeout, an operator, or the next runner reaping what the last one left.)

The row is left in ``running`` for the same reason: because it is. Writing
``cancelled`` over a container that is still going would be the first false
statement in that table, and the row does not need this path — the sweep
(``jhin_tool_worker.sandbox_reconcile``) closes it from the runner's own
account of the job, with the real exit code, or as ``runner_gone`` if the
runner has gone too. What this path writes is the one thing it actually
knows: that the worker walked away.

These tests are about the row, the trail and the container, not about the
tool call: the cancellation itself must still reach the caller, because a
worker that is leaving has to leave.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jhin_connectors.cli import tools as cli_tools
from jhin_connectors.cli.schemas import FileListInput
from jhin_db.base import Base
from jhin_db.models import AuditEvent, Connection, SandboxJob, Workspace
from jhin_domain import SandboxJobStatus, new_uuid7
from jhin_secrets import SecretCrypto
from jhin_secrets.crypto import MasterKey, decode_master_key_material, generate_master_key_material
from jhin_tools.builtin import ToolExecutionContext
from jhin_tools.errors import ToolExecutionError


class _Durable:
    """A context whose evidence writes reach their own connection, which is
    the arrangement the tool worker actually runs — and the only one where
    "close the row while being cancelled" means anything."""

    def __init__(self, engine: Any, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.engine = engine
        self.sessions = sessions
        self.workspace_id: UUID
        self.connection_id: UUID
        self.tool_call_id: UUID

    async def context(self, session: AsyncSession) -> ToolExecutionContext:
        return ToolExecutionContext(
            session=session,
            workspace_id=self.workspace_id,
            task_id=new_uuid7(),
            run_id=new_uuid7(),
            agent_id=new_uuid7(),
            agent_name="Scout",
            crypto=SecretCrypto(
                MasterKey(key=decode_master_key_material(generate_master_key_material()))
            ),
            session_factory=self.sessions,
            tool_call_id=self.tool_call_id,
        )

    async def job_rows(self) -> list[SandboxJob]:
        async with self.sessions() as verify:
            return list(await verify.scalars(select(SandboxJob)))

    async def audit_actions(self) -> list[str]:
        async with self.sessions() as verify:
            rows = await verify.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
            )
            return [row.action for row in rows]


@pytest.fixture
async def durable(tmp_path: Any) -> Any:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    world = _Durable(engine, sessions)
    async with sessions() as setup:
        workspace = Workspace(name="Shutdown", slug=f"shutdown-{new_uuid7().hex[:8]}")
        setup.add(workspace)
        await setup.flush()
        connection_row = Connection(
            workspace_id=workspace.id,
            connector_type="cli",
            name="cli",
            auth_type="none",
            status="active",
            config_json={"default_image": "jhin-sandbox:latest", "default_network": "none"},
        )
        setup.add(connection_row)
        await setup.commit()
        world.workspace_id = workspace.id
        world.connection_id = connection_row.id
        world.tool_call_id = new_uuid7()
    yield world
    await engine.dispose()


async def _cancelled_listing(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def killed_mid_job(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict:
        raise asyncio.CancelledError

    monkeypatch.setattr(cli_tools, "run_sandbox_job", killed_mid_job)

    async with durable.sessions() as session:
        context = await durable.context(session)
        with pytest.raises(asyncio.CancelledError):
            await cli_tools._file_list(
                context,
                FileListInput(connection_id=str(durable.connection_id)),
            )


async def test_an_abandoned_job_keeps_its_container_and_its_running_row(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _cancelled_listing(durable, monkeypatch)

    [row] = await durable.job_rows()
    # Still running, because it still is. The sweep closes it from the
    # runner's account of the container, which is a better ending than any
    # guess this process could write on its way out.
    assert row.status == SandboxJobStatus.RUNNING.value
    assert row.completed_at is None
    # And the trail says what this path actually knows.
    actions = await durable.audit_actions()
    assert actions[-2:] == ["sandbox.job.started", "sandbox.job.abandoned"]


async def test_the_abandonment_names_its_reason(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator reading the trail afterwards has to be able to tell this
    apart from a job that simply has not finished yet."""
    await _cancelled_listing(durable, monkeypatch)

    async with durable.sessions() as verify:
        rows = list(await verify.scalars(select(AuditEvent)))
    [note] = [row for row in rows if row.action == "sandbox.job.abandoned"]
    assert note.metadata_json["reason"] == "worker_shutdown"
    assert "left running" in note.metadata_json["evidence"]


async def test_nothing_asks_the_runner_to_kill_the_container(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this file exists to hold closed. The connector no longer
    imports the cancel at all, so there is nothing here that could ask."""
    assert not hasattr(cli_tools, "cancel_sandbox_job")

    await _cancelled_listing(durable, monkeypatch)

    [row] = await durable.job_rows()
    assert row.status == SandboxJobStatus.RUNNING.value


async def test_a_dropped_runner_retains_its_row_for_confirmation(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug the cancellation work uncovered, kept closed.

    Every failure ending sets one output tail and leaves the other as it was
    — ``None`` on the in-memory row, because the column's default applies on
    insert. Both are NOT NULL, so the terminal UPDATE was refused, the
    refusal was swallowed on purpose (a lost record must not fail a tool
    call), and the row stayed ``running`` for good. It failed for the runner
    error path exactly as it failed for the cancellation path.
    """

    async def dropped(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict:
        raise cli_tools.SandboxRunnerError("sandbox runner unreachable: ConnectError")

    monkeypatch.setattr(cli_tools, "run_sandbox_job", dropped)

    async with durable.sessions() as session:
        context = await durable.context(session)
        with pytest.raises(ToolExecutionError) as raised:
            await cli_tools._file_list(
                context,
                FileListInput(connection_id=str(durable.connection_id)),
            )
    # A read itself has no effects, but its container still holds the disk.
    assert raised.value.side_effect_possible is False
    [row] = await durable.job_rows()
    assert (row.status, row.error_code) == (SandboxJobStatus.RUNNING.value, "runner_error")
    assert row.completed_at is None


async def test_a_note_that_hangs_is_abandoned_inside_its_budget(
    durable: _Durable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown is on Docker's clock, not ours. A database that has stopped
    answering must cost the shutdown its small budget and not a second more —
    the note is only a note, and the sweep closes the row either way."""

    async def killed_mid_job(payload: dict[str, Any], *, job_timeout_seconds: int) -> dict:
        raise asyncio.CancelledError

    async def never_answers(evidence: Any, job_id: str) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(cli_tools, "run_sandbox_job", killed_mid_job)
    monkeypatch.setattr(cli_tools, "_write_abandoned", never_answers)
    monkeypatch.setattr(cli_tools, "_CANCEL_CLEANUP_SECONDS", 0.05)

    loop = asyncio.get_running_loop()
    started = loop.time()
    async with durable.sessions() as session:
        context = await durable.context(session)
        with pytest.raises(asyncio.CancelledError):
            await cli_tools._file_list(
                context,
                FileListInput(connection_id=str(durable.connection_id)),
            )
    assert loop.time() - started < 5.0
