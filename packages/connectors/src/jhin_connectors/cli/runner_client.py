"""HTTP client for the internal sandbox runner API (plan 14.2).

Runs inside the tool worker only — ``SANDBOX_RUNNER_URL`` and
``SANDBOX_RUNNER_TOKEN`` are set on that service alone (compose.yaml), and the
agent worker is forbidden from importing this module by
``tests/test_worker_dependency_boundaries.py``. The ``runner`` compose network
is the wall, the shared bearer token is the lock.
Submit is fire-and-poll — the runner answers 202 immediately and the client
polls status until the job reaches a terminal state or the client-side
deadline passes (a backstop; the runner enforces the real per-job timeout).

There is deliberately no "cancel this job because I am leaving" call here any
more. A worker on its way out leaves the container alone: it is the only thing
that still knows what the tool call did, and the re-dispatch that follows a
redeploy gets that outcome from the runner instead of inheriting a workspace
stopped halfway through a write. The only cancel left is the one the deadline
sends, which is a job that is genuinely past every budget it was given.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any, cast

import httpx

DEFAULT_RUNNER_URL = "http://sandbox-runner:8085"
_POLL_INTERVAL_SECONDS = 1.0
# Grace on top of the job's own timeout *and* the runner's reported pre-start
# budget, before the client gives up. It covers only what neither of those
# does: the poll interval, the runner's kill, and collecting the container's
# logs afterwards.
_DEADLINE_GRACE_SECONDS = 60.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})
_ProgressObserver = Callable[[dict[str, Any]], Awaitable[None]]
_PROGRESS: ContextVar[_ProgressObserver | None] = ContextVar("sandbox_progress", default=None)


@contextlib.contextmanager
def sandbox_job_progress(observer: _ProgressObserver | None) -> Iterator[None]:
    """A task-local best-effort observer; it cannot submit or retry a job."""
    token = _PROGRESS.set(observer)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


async def _report_progress(snapshot: dict[str, Any]) -> None:
    observer = _PROGRESS.get()
    if observer is not None:
        # Losing a progress write must neither change a job's outcome nor
        # turn a caller shutdown into cancellation of the running container.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(0.5):
                await observer(dict(snapshot))


class SandboxRunnerError(Exception):
    """The runner is unreachable or rejected the request. Messages are safe
    to persist and show to models — never secret material."""


class SandboxInvocationUnknownError(SandboxRunnerError):
    """The runner refused to run this call again because it cannot say what
    the earlier attempt did.

    A subclass, so a caller that only wants "the runner did not run this" is
    already right; a caller that has to explain the outcome to a person asks
    for this specifically. Nothing was started — the refusal happens before
    the job record exists — but the *earlier* dispatch may have got as far as
    it liked, which is precisely what is unknown.
    """


def runner_config() -> tuple[str, str]:
    """(base_url, token) from the process environment."""
    url = os.environ.get("SANDBOX_RUNNER_URL", "").strip() or DEFAULT_RUNNER_URL
    token = os.environ.get("SANDBOX_RUNNER_TOKEN", "").strip()
    return url.rstrip("/"), token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def workspace_file_operation(
    workspace_key: str, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Execute one contained operation; never automatically repeat an uncertain write."""
    import re

    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,81}", workspace_key):
        raise SandboxRunnerError("Invalid workspace identity")
    url, token = runner_config()
    try:
        async with httpx.AsyncClient(timeout=100) as client:
            response = await client.post(
                f"{url}/v1/workspaces/{workspace_key}/operation",
                headers=_headers(token),
                json={"operation": operation, "args": args},
            )
        if response.is_error:
            detail = response.json().get("detail", "Workspace operation refused")
            raise SandboxRunnerError(str(detail)[:300])
        return cast(dict[str, Any], response.json())
    except httpx.HTTPError as exc:
        raise SandboxRunnerError(
            "Workspace operation connection lost; inspect the current file before repeating a write"
        ) from exc


async def run_sandbox_job(
    request_payload: dict[str, Any], *, job_timeout_seconds: int
) -> dict[str, Any]:
    """Submit one job and poll it to a terminal state.

    Returns the runner's final status document. Raises
    :class:`SandboxRunnerError` for transport/validation failures — the job
    either never started or was cancelled as part of the failure path — and
    :class:`SandboxInvocationUnknownError` when the runner refuses to repeat a
    dispatch whose earlier attempt it cannot account for.

    **The job polled is the job the runner names, not the one submitted.**
    When the payload carries an ``invocation_id`` the runner may answer with
    an earlier dispatch's job — that is how a re-dispatch attaches to the
    first attempt instead of starting a second container — and polling the id
    we sent would then wait forever on a job that does not exist. The returned
    document reports the id that was actually followed, so a caller can tell
    the two apart.

    **The deadline covers the runner's own overhead, because the runner says
    what that overhead is.** It used to be the job's timeout plus a flat
    minute, chosen when the only thing spent before a container started was a
    moment. It is now a queue for the workspace volume and a walk of that
    volume, together up to a minute and a half — so the flat minute was
    entirely consumed before the job began, and the client cancelled and
    killed containers that were doing exactly what they were asked to. The
    runner reports its pre-start budget on the submit response and it is added
    here, so nobody has to remember to widen a constant in another repository
    directory when they widen one of those.
    """
    base_url, token = runner_config()
    if not token:
        raise SandboxRunnerError("SANDBOX_RUNNER_TOKEN is not configured in this worker")
    requested_job_id = str(request_payload.get("job_id", ""))

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        try:
            response = await client.post("/v1/jobs", json=request_payload, headers=_headers(token))
        except httpx.HTTPError as exc:
            raise SandboxRunnerError(f"sandbox runner unreachable: {type(exc).__name__}") from exc
        if response.status_code == 409:
            raise SandboxInvocationUnknownError(
                f"sandbox runner would not repeat this call: {response.text[:300]}"
            )
        if response.status_code not in (200, 202):
            raise SandboxRunnerError(
                f"sandbox runner rejected the job ({response.status_code}): {response.text[:300]}"
            )
        try:
            accepted = response.json() if response.content else {}
        except ValueError as exc:
            raise SandboxRunnerError("sandbox runner returned an unreadable submit answer") from exc
        if not isinstance(accepted, dict):
            raise SandboxRunnerError("sandbox runner returned an unexpected submit shape")
        job_id = str(accepted.get("job_id") or requested_job_id)
        pre_start = accepted.get("pre_start_budget_seconds")
        grace = float(pre_start) if isinstance(pre_start, int | float) else 0.0
        deadline = (
            asyncio.get_event_loop().time() + job_timeout_seconds + grace + _DEADLINE_GRACE_SECONDS
        )

        while True:
            status_doc = await _job_status(client, token, job_id)
            if status_doc.get("status") in _TERMINAL_STATUSES:
                status_doc["polled_job_id"] = job_id
                return status_doc
            await _report_progress(status_doc)
            if asyncio.get_event_loop().time() > deadline:
                await _try_cancel(client, token, job_id)
                raise SandboxRunnerError(
                    f"job {job_id} did not reach a terminal state within the deadline"
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _job_status(client: httpx.AsyncClient, token: str, job_id: str) -> dict[str, Any]:
    try:
        response = await client.get(f"/v1/jobs/{job_id}", headers=_headers(token))
    except httpx.HTTPError as exc:
        raise SandboxRunnerError(f"sandbox runner unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise SandboxRunnerError(f"job {job_id} status lookup failed ({response.status_code})")
    document = response.json()
    if not isinstance(document, dict):
        raise SandboxRunnerError("sandbox runner returned an unexpected status shape")
    return document


async def _try_cancel(client: httpx.AsyncClient, token: str, job_id: str) -> None:
    # Best effort; the runner's own timeout will reap the job regardless.
    with contextlib.suppress(httpx.HTTPError):
        await client.post(f"/v1/jobs/{job_id}/cancel", headers=_headers(token))


async def sandbox_job_state(job_id: str) -> dict[str, Any] | None:
    """What the runner currently says about one job, or ``None`` if it has
    never heard of it.

    The distinction is the whole value of this call, and it is why the sweep
    can be safe. The runner's registry is in memory: it holds every job the
    *running* runner process submitted, finished or not, and nothing from
    before its last restart. So an answer of ``running`` is proof the job is
    genuinely still going and must be left alone, a terminal answer is the
    real outcome to record, and ``None`` — a 404 — is proof that whatever was
    running this job is gone and nothing will ever finish it.

    Raises :class:`SandboxRunnerError` when the runner cannot be reached or
    answers something else. That is deliberately not the same as ``None``:
    "I cannot ask" must never be mistaken for "it is not there".
    """
    base_url, token = runner_config()
    if not token:
        raise SandboxRunnerError("SANDBOX_RUNNER_TOKEN is not configured in this worker")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            response = await client.get(f"/v1/jobs/{job_id}", headers=_headers(token))
    except httpx.HTTPError as exc:
        raise SandboxRunnerError(f"sandbox runner unreachable: {type(exc).__name__}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise SandboxRunnerError(f"job {job_id} status lookup failed ({response.status_code})")
    document = response.json()
    if not isinstance(document, dict):
        raise SandboxRunnerError("sandbox runner returned an unexpected status shape")
    return document


async def runner_memory() -> dict[str, Any]:
    """What the runner can still be asked about: ``serving_since`` and
    ``job_record_retention_seconds``.

    The companion to :func:`sandbox_job_state`, and the reason that function's
    ``None`` is safe to act on. A 404 there is two different facts — a runner
    that restarted and reaped the container, and a runner that ran the job,
    finished it and has since dropped the record — and only the first says no
    outcome is coming. These two values are where the boundary between them
    is, held by the process that knows it.

    Raises :class:`SandboxRunnerError` when the runner cannot be reached or
    will not answer, which includes a runner too old to have this endpoint.
    That is the honest failure: a caller that cannot establish the boundary
    must not close a row on the strength of a 404, and this raises rather than
    returning a default that would let it.
    """
    base_url, token = runner_config()
    if not token:
        raise SandboxRunnerError("SANDBOX_RUNNER_TOKEN is not configured in this worker")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            response = await client.get("/v1/runner/memory", headers=_headers(token))
    except httpx.HTTPError as exc:
        raise SandboxRunnerError(f"sandbox runner unreachable: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise SandboxRunnerError(
            f"sandbox runner would not say what it remembers ({response.status_code})"
        )
    document = response.json()
    if not isinstance(document, dict):
        raise SandboxRunnerError("sandbox runner returned an unexpected memory shape")
    return document


async def delete_workspace(workspace_key: str) -> bool:
    """Destroy one persistent workspace volume (run finalize, plan 14.5).

    Best-effort by contract: returns False instead of raising so finalize
    never fails because cleanup did. True means the volume is *gone* and
    nothing else: the runner answers 204 both for a volume it removed and for
    one that was never there, which are the same answer to the only question
    a caller has. A 409 is the runner saying Docker refused because something
    still has the volume mounted, and it is False -- reading a refusal as
    success is how a workspace that is still full gets recorded as empty and
    stops being visible to the cap, to eviction and to the operator who asked
    for it to be cleared. 404 stays accepted for a resource that is not there
    to delete at all.
    """
    base_url, token = runner_config()
    if not token:
        return False
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            response = await client.delete(
                f"/v1/workspaces/{workspace_key}", headers=_headers(token)
            )
        return response.status_code in (204, 404)
    except httpx.HTTPError:
        return False
