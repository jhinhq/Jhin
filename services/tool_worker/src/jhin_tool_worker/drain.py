"""Shutting the tool worker down without abandoning what it was doing.

Docker sends SIGTERM and then, a few seconds later, SIGKILL. What the worker
did with that warning was nothing: the signal handler released the main
coroutine, the Temporal worker's shutdown cancelled every running activity on
the spot (its graceful window is zero), and a tool call that was halfway
through a sandbox job was simply cut off. The gateway, unable to prove what
the executor had done, recorded ``execution_unknown``; the run failed; the
``sandbox_job`` row stayed ``running`` with nobody left to finish it.

Two things make the difference, and this module is both of them:

* **Stop taking new work.** A call the worker accepts one second before
  SIGKILL is a call it will dispatch and then abandon, which is the worst
  outcome available — a claim in ``executing`` that nothing can vouch for.
  Refusing it before anything is claimed is a clean no-op, and Temporal
  redelivers it to whichever worker is alive to run it.
* **Let what is in flight finish.** The main loop waits for the in-flight
  count to reach zero before it starts the Temporal shutdown, so a listing or
  a file read that needed another second gets it. The wait is bounded by the
  stop grace, not by hope: whatever has not finished is still cancelled, and
  the cancellation path records that it walked away — leaving the container
  running, because that container is the only thing that still knows what the
  call did, and the re-dispatch which follows this shutdown will ask the
  runner for exactly that.

The counter is deliberately not a semaphore or a lock. It counts, and the
only thing that reads it is a shutdown deciding whether it may proceed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta

from temporalio.exceptions import ApplicationError

#: The clock everything in this file is spent against: Docker's default stop
#: grace, which is what a ``docker compose`` service gets when no
#: ``stop_grace_period`` is set — and none is set for ``tool-worker``. SIGTERM
#: at zero, SIGKILL at ten, and nothing after the ten is negotiable.
#:
#: The shutdown spends that grace in three consecutive budgets, and they are
#: deliberately three rather than one because they belong to three different
#: pieces of code:
#:
#: * :data:`DRAIN_BUDGET_SECONDS` — waiting for in-flight tool calls (here);
#: * ``jhin_connectors.cli.tools._CANCEL_CLEANUP_SECONDS`` — per abandoned
#:   job, recording that this worker let go of it while being cancelled;
#: * :data:`RUNTIME_SHUTDOWN_BUDGET_SECONDS` — flushing telemetry in
#:   ``jhin_tool_worker.main``.
#:
#: They used to sum to exactly ten with the Temporal worker's own exit and the
#: telemetry flush still to come, which is not a budget but a coincidence: any
#: job that outlived the drain was racing SIGKILL, and the loser left the very
#: orphan the drain exists to prevent. They now sum to eight, and the two
#: seconds left over are the margin — for ``worker.__aexit__``, for the
#: connection pool, and for the arithmetic being wrong by a little.
#:
#: The margin is not the guarantee, and nothing here pretends otherwise. A job
#: that needs longer than the drain is *still* cut off, and the honest backstop
#: for it is not this file at all: it is the sandbox sweep
#: (:mod:`jhin_tool_worker.sandbox_reconcile`), which closes the row from the
#: runner's own account of the job however the worker died — SIGKILL included,
#: where no shutdown path of any length runs. What these budgets buy is that
#: the ordinary redeploy does not need the backstop.
STOP_GRACE_SECONDS = 10.0
#: How long the drain waits for in-flight calls. The default for
#: ``ToolWorkerSettings.tool_worker_drain_timeout_seconds``.
DRAIN_BUDGET_SECONDS = 4.0
#: How long the telemetry runtime gets to flush after everything else is shut.
RUNTIME_SHUTDOWN_BUDGET_SECONDS = 2.0
#: What is deliberately left unspent, for the steps that have no budget of
#: their own: the Temporal worker's exit and the connection pool's close.
SHUTDOWN_MARGIN_SECONDS = 2.0

#: What the refusal is called on the wire, for a workflow deciding what to do
#: with it. Retryable: the work is fine, this process is not.
DRAINING_ERROR_TYPE = "tool_worker_draining"
#: How long Temporal is asked to wait before trying this call again.
#:
#: Not decoration: it is what keeps a redeploy from eating an activity's whole
#: retry budget in six seconds. The refusal is instant, so with the step
#: policy's own 2s/4s backoff all three attempts of a turn used to land inside
#: one drain window and the run died of a restart it was supposed to survive.
#: Longer than the drain, so at most one attempt can be spent on a draining
#: worker; after that the process is gone, nothing polls this queue, and the
#: task simply waits for a live worker without consuming an attempt at all.
DRAINING_RETRY_DELAY = timedelta(seconds=STOP_GRACE_SECONDS)
#: The sentence a person ends up reading if every retry lands on a draining
#: worker. It says what happened and that nothing was started, because "an
#: activity failed" tells them neither.
DRAINING_MESSAGE = "the tool worker was restarting and did not start this tool call; nothing ran"


class WorkerDrain:
    """In-flight accounting plus the one-way switch that stops new work."""

    def __init__(self) -> None:
        self._draining = False
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def begin(self) -> None:
        """Refuse everything from here on. One way, on purpose: a process
        that has been told to stop does not get to change its mind."""
        self._draining = True

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Account for one activity, or refuse it because we are leaving.

        The refusal happens *before* the body runs, which is the whole point:
        nothing has been claimed, nothing dispatched, and there is nothing for
        anyone to reconcile. A call that gets past this line is one the
        shutdown will wait for.
        """
        if self._draining:
            raise ApplicationError(
                DRAINING_MESSAGE,
                type=DRAINING_ERROR_TYPE,
                next_retry_delay=DRAINING_RETRY_DELAY,
            )
        self._in_flight += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._in_flight -= 1
            if self._in_flight <= 0:
                self._idle.set()

    async def wait_idle(self, budget_seconds: float) -> int:
        """Wait for the in-flight work to end. Returns what is still running.

        A non-zero answer is not an error, it is the honest report the caller
        logs before cancelling the rest: the budget belongs to Docker, and
        pretending otherwise would only mean being killed mid-sentence.
        """
        if budget_seconds <= 0:
            return self._in_flight
        with suppress(TimeoutError):
            async with asyncio.timeout(budget_seconds):
                await self._idle.wait()
        return self._in_flight


__all__ = [
    "DRAINING_ERROR_TYPE",
    "DRAINING_MESSAGE",
    "DRAINING_RETRY_DELAY",
    "DRAIN_BUDGET_SECONDS",
    "RUNTIME_SHUTDOWN_BUDGET_SECONDS",
    "SHUTDOWN_MARGIN_SECONDS",
    "STOP_GRACE_SECONDS",
    "WorkerDrain",
]
