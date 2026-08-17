"""HTTP client for the internal sandbox runner API (plan 14.2).

Runs inside the agent worker only: the ``runner`` compose network is the
wall, the shared bearer token from ``SANDBOX_RUNNER_TOKEN`` is the lock.
Submit is fire-and-poll — the runner answers 202 immediately and the client
polls status until the job reaches a terminal state or the client-side
deadline passes (a backstop; the runner enforces the real per-job timeout).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import httpx

DEFAULT_RUNNER_URL = "http://sandbox-runner:8085"
_POLL_INTERVAL_SECONDS = 1.0
# Grace on top of the job's own timeout before the client gives up.
_DEADLINE_GRACE_SECONDS = 60.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


class SandboxRunnerError(Exception):
    """The runner is unreachable or rejected the request. Messages are safe
    to persist and show to models — never secret material."""


def runner_config() -> tuple[str, str]:
    """(base_url, token) from the process environment."""
    url = os.environ.get("SANDBOX_RUNNER_URL", "").strip() or DEFAULT_RUNNER_URL
    token = os.environ.get("SANDBOX_RUNNER_TOKEN", "").strip()
    return url.rstrip("/"), token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def run_sandbox_job(
    request_payload: dict[str, Any], *, job_timeout_seconds: int
) -> dict[str, Any]:
    """Submit one job and poll it to a terminal state.

    Returns the runner's final status document. Raises
    :class:`SandboxRunnerError` for transport/validation failures — the job
    either never started or was cancelled as part of the failure path.
    """
    base_url, token = runner_config()
    if not token:
        raise SandboxRunnerError("SANDBOX_RUNNER_TOKEN is not configured in this worker")
    job_id = str(request_payload.get("job_id", ""))
    deadline = asyncio.get_event_loop().time() + job_timeout_seconds + _DEADLINE_GRACE_SECONDS

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        try:
            response = await client.post("/v1/jobs", json=request_payload, headers=_headers(token))
        except httpx.HTTPError as exc:
            raise SandboxRunnerError(f"sandbox runner unreachable: {type(exc).__name__}") from exc
        if response.status_code not in (200, 202):
            raise SandboxRunnerError(
                f"sandbox runner rejected the job ({response.status_code}): {response.text[:300]}"
            )

        while True:
            status_doc = await _job_status(client, token, job_id)
            if status_doc.get("status") in _TERMINAL_STATUSES:
                return status_doc
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


async def delete_workspace(workspace_key: str) -> bool:
    """Destroy one persistent workspace volume (run finalize, plan 14.5).
    Best-effort by contract: returns False instead of raising so finalize
    never fails because cleanup did."""
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
