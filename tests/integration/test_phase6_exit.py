"""Phase 6 exit test (plan 45): a coding agent works in an ephemeral sandbox.

The full SWE loop against the running stack, with zero real credentials:
an agent granted scoped cli.* capabilities plus the GitHub PR tool

1. checks out the seeded test repo (git smart-HTTP on fake-github) onto the
   run's workspace volume, on a fresh agent branch,
2. reads the broken file, runs the seeded test script (fails),
3. writes the fix, reruns the tests (passes),
4. commits and pushes the branch (short-lived token via askpass, network
   `internet` = the sandbox bridge),
5. opens a PR through the existing GitHub connector.

Assertions: task completed; every tool_call and sandbox_job row completed;
the pushed branch is visible in the git server's refs (via the fake GitHub
state); the PR exists; the PAT never appears in persisted output; sandbox
containers and the run's workspace volume are gone afterwards.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD

from .conftest import API_URL, FAKE_GITHUB_URL, REPO_ROOT, compose

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
FAKE_GITHUB_INTERNAL = "http://fake-github:8080"
FAKE_GITHUB_HOST = FAKE_GITHUB_URL
FAKE_GITHUB_PAT = "fake-github-pat"
# Sandbox jobs are real containers; the whole flow needs more headroom than
# the API-only phase 5 tasks.
TASK_TIMEOUT_SECONDS = 300.0


@pytest.fixture
async def owner() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        credentials = {"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
        login = await client.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        yield client, login.json()["memberships"][0]["workspace_id"]


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("jhin_csrf")
    assert token, "no CSRF cookie after login"
    return {"x-csrf-token": token}


async def _post(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], expect: int = 201
) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params or None)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    return response.json()


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str) -> dict[str, Any]:
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P6 provider {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P6 profile {tag}",
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"P6 coder {tag}",
            "system_prompt": "You are a software engineer; use tools when instructed.",
            "model_profile_id": profile["id"],
        },
    )


async def _run_task(
    client: httpx.AsyncClient, ws: str, agent_id: str, title: str, description: str
) -> dict[str, Any]:
    task = await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/assign-task",
        {"title": title, "description": description},
    )
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}")
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(1.0)
    pytest.fail(f"task {task['id']} did not finish in {TASK_TIMEOUT_SECONDS}s: {detail}")


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout.strip()


def _psql(sql: str) -> str:
    """Row-per-line query output straight from the stack's Postgres."""
    result = compose("exec", "-T", "postgres", "psql", "-U", "jhin", "-d", "jhin", "-tA", "-c", sql)
    return result.stdout.strip()


async def test_swe_agent_full_sandbox_flow(owner: tuple[httpx.AsyncClient, str]) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]

    # --- wiring: GitHub connection (REST + git), CLI connection, agent, grants
    github = (
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/connections",
            {
                "connector_type": "github",
                "name": f"P6 GitHub {tag}",
                "auth_type": "pat",
                "credentials": {"token": FAKE_GITHUB_PAT},
                "config": {"base_url": FAKE_GITHUB_INTERNAL},
            },
        )
    )["connection"]
    cli = (
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/connections",
            {
                "connector_type": "cli",
                "name": f"P6 CLI {tag}",
                "auth_type": "none",
                "credentials": {},
                "config": {"default_network": "none", "git_connection_id": github["id"]},
            },
        )
    )["connection"]

    agent = await _make_agent(client, ws, tag)
    grants = {
        "cli.repository.checkout": {"connection_id": cli["id"], "repository": "octo/alpha"},
        "cli.file.read": {"connection_id": cli["id"], "path": "*"},
        "cli.file.write": {"connection_id": cli["id"], "path": "*"},
        "cli.test.run": {"connection_id": cli["id"], "command": "bash *"},
        "cli.command.execute": {"connection_id": cli["id"], "command": "git *"},
        "github.pull_request.create": {"connection_id": github["id"], "repository": "octo/alpha"},
    }
    for capability, scope in grants.items():
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/grants",
            {"capability": capability, "scope": scope, "effect": "allow"},
        )

    # --- the scripted SWE flow (fake provider replays markers in order)
    branch = f"agent/p6-{tag}"
    cli_id = cli["id"]
    push_command = f"git add -A && git commit -m fix-value && git push origin {branch}"
    markers = " ".join(
        [
            f'[[tool:cli.repository.checkout {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "branch": "{branch}"}}]]',
            f'[[tool:cli.file.read {{"connection_id": "{cli_id}", "path": "app.py"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:cli.file.write {{"connection_id": "{cli_id}", "path": "app.py", '
            f'"content": "VALUE = 2\\n"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:cli.command.execute {{"connection_id": "{cli_id}", '
            f'"command": "{push_command}", "network": "internet"}}]]',
            f'[[tool:github.pull_request.create {{"connection_id": "{github["id"]}", '
            f'"repository": "octo/alpha", "title": "Fix VALUE {tag}", '
            f'"head": "{branch}", "base": "main", "body": "Automated by Jhin."}}]]',
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Fix the failing test {tag}", f"Do the work: {markers}"
    )
    assert detail["task"]["state"] == "completed", detail
    run_id = detail["runs"][0]["id"]

    # --- every tool call completed, in the scripted order
    calls = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls")
    assert [c["tool_name"] for c in calls] == [
        "cli.repository.checkout",
        "cli.file.read",
        "cli.test.run",
        "cli.file.write",
        "cli.test.run",
        "cli.command.execute",
        "github.pull_request.create",
    ], calls
    assert all(c["status"] == "completed" for c in calls), calls

    checkout_out = calls[0]["sanitized_output_json"]
    assert checkout_out["branch"] == branch
    assert checkout_out["head_sha"], "checkout must report the cloned HEAD sha"
    assert calls[1]["sanitized_output_json"]["content"].startswith("VALUE = 1")
    assert calls[2]["sanitized_output_json"]["passed"] is False  # red before the fix
    assert calls[4]["sanitized_output_json"]["passed"] is True  # green after the fix
    assert calls[5]["sanitized_output_json"]["exit_code"] == 0
    pr_number = calls[6]["sanitized_output_json"]["number"]

    # The short-lived git token never persists anywhere (plan 48.9).
    assert FAKE_GITHUB_PAT not in json.dumps(calls)

    # --- sandbox_job rows: one per cli.* call, all completed, linked to the run
    rows = _psql(
        f"select status, exit_code from sandbox_job where run_id = '{run_id}' order by id"
    ).splitlines()
    assert len(rows) == 6, rows  # checkout, read, test, write, test, push
    assert all(row.startswith("completed|") for row in rows), rows
    leaked = _psql(
        "select count(*) from sandbox_job where run_id = "
        f"'{run_id}' and (stdout_tail like '%{FAKE_GITHUB_PAT}%' "
        f"or stderr_tail like '%{FAKE_GITHUB_PAT}%')"
    )
    assert leaked == "0", "PAT leaked into persisted sandbox job output"

    # --- sandbox.job events feed the task timeline (plan 45 Phase 6 UI)
    timeline = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run_id}/timeline")
    sandbox_events = [e for e in timeline if e["event_type"] == "sandbox.job"]
    assert len(sandbox_events) == 6, [e["event_type"] for e in timeline]
    assert all(e["payload_json"]["job_status"] == "completed" for e in sandbox_events)

    # --- the pushed branch is real: visible in the git server's refs
    async with httpx.AsyncClient(timeout=10.0) as anon:
        state = (await anon.get(f"{FAKE_GITHUB_HOST}/_state")).json()
    repo = state["repos"]["octo/alpha"]
    assert branch in repo["branches"], sorted(repo["branches"])
    assert repo["branches"][branch] != checkout_out["head_sha"], "push must add a new commit"

    # --- and the PR exists, from our branch into main
    pull = repo["pulls"][str(pr_number)]
    assert pull["title"] == f"Fix VALUE {tag}"
    assert pull["head"]["ref"] == branch
    assert pull["base"]["ref"] == "main"

    # --- nothing left behind: no job containers, no run workspace volume
    job_ids = _psql(f"select id from sandbox_job where run_id = '{run_id}'").splitlines()
    for job_id in job_ids:
        assert _docker("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}") == ""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:  # finalize cleanup is async wrt task state
        volumes = _docker(
            "volume", "ls", "-q", "--filter", f"label=jhin.sandbox.workspace=run-{run_id}"
        )
        if volumes == "":
            break
        await asyncio.sleep(1.0)
    assert volumes == "", "run workspace volume must be deleted at finalize"
