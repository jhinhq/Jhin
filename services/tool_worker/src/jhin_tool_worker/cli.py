"""Operator entry points for the tool worker.

Two commands:

``jhin-sandbox-reconcile``
    One pass of :mod:`jhin_tool_worker.sandbox_reconcile`, printing what it
    did. The worker runs the same sweep on its own schedule; this is the
    handle for a person who knows about a stuck job and does not want to wait
    for the next pass.
``jhin-tool-calls-rollback``
    One pass of :mod:`jhin_tool_worker.rollback`, which closes every
    ``claimed`` tool call so the *previous* release can be started without
    meeting a status it has never heard of. It is the one manual step in
    rolling this change back, and the module says why it is a step at all.

They live in this service rather than in ``jhin-admin`` because of topology
rather than taste. The sweep's whole safety argument rests on asking the
sandbox runner whether it still knows the job, and the runner is reachable
only from the ``runner`` compose network — which the API container is
deliberately not on. The tool worker is the one process that can both read
the rows and ask the question, and the rollback belongs beside it because it
is the same lifecycle.

Stdout is the interface here, the way it is for ``jhin-db-migrate`` and
``jhin-catalog-sync``: a person or a scheduler reads it, so it is plain text
rather than a structured log record.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jhin_db import create_engine, create_session_factory
from jhin_observability import noop_tracer
from jhin_tool_worker.rollback import close_claimed_tool_calls
from jhin_tool_worker.sandbox_reconcile import reconcile_orphaned_sandbox_jobs
from jhin_tool_worker.settings import ToolWorkerSettings


@asynccontextmanager
async def _operator_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The one database engine either command here opens, disposed on the way out.

    Both commands want exactly this and nothing else: settings from the
    environment the worker already runs in, one engine, and no runtime behind
    it. Kept in one place so this module has a single engine site — which is
    what ``packages/observability/tests/test_temporal.py`` asserts, by name,
    for every process in the repo that opens one.

    No observability runtime is bootstrapped for a one-shot command, so SQL
    tracing has nothing to report to; ``jhin-admin`` and the dev seed pass the
    same no-op. Named rather than left to the default, because every engine in
    this repo says out loud which tracer it got.
    """
    settings = ToolWorkerSettings()
    engine = create_engine(settings.database_url, tracer=noop_tracer())
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def reconcile_sandbox_jobs() -> int:
    """Run one sweep and report it. Exit 0 whether or not it closed anything:
    "nothing to reconcile" is a success, not a silence."""
    async with _operator_session_factory() as session_factory:
        report = await reconcile_orphaned_sandbox_jobs(session_factory)
    print(report.describe())
    return 0


def run_reconcile_sandbox_jobs() -> int:
    return asyncio.run(reconcile_sandbox_jobs())


async def rollback_claimed_tool_calls(*, dry_run: bool = False) -> int:
    """Close every ``claimed`` tool call, or list what would be closed.

    Exit 0 either way, including when there is nothing to close: an empty
    table is the normal answer, and a rollback script should not have to
    distinguish "nothing to do" from "it worked".
    """
    async with _operator_session_factory() as session_factory:
        report = await close_claimed_tool_calls(session_factory, dry_run=dry_run)
    print(report.describe())
    return 0


def run_rollback_claimed_tool_calls() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    return asyncio.run(rollback_claimed_tool_calls(dry_run=dry_run))


__all__ = [
    "reconcile_sandbox_jobs",
    "rollback_claimed_tool_calls",
    "run_reconcile_sandbox_jobs",
    "run_rollback_claimed_tool_calls",
]


if __name__ == "__main__":
    raise SystemExit(run_reconcile_sandbox_jobs())
