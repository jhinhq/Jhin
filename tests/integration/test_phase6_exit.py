"""Phase 6 exit test (plan 45): a coding agent works in an ephemeral sandbox.

The full SWE loop against the running stack, with zero real credentials: an
agent granted scoped cli.* capabilities plus the GitHub PR tool

1. checks out the seeded test repo (git smart-HTTP on fake-github) onto the
   run's workspace volume, on a fresh agent branch,
2. finds its way around a repository nobody handed it a file path for —
   lists, searches, reads a page,
3. runs the seeded test script (fails), edits the one line that is wrong,
   reruns the tests (passes),
4. pushes the branch through ``cli.repository.push`` — a Jhin-authored script
   holding the credential, gated on a human approval,
5. opens a PR through the existing GitHub connector.

Assertions: task completed; every tool_call and sandbox_job row completed; the
pushed branch is visible in the git server's refs; the PR exists; the PAT never
appears in persisted output; sandbox containers and the run's workspace volume
are gone afterwards.

The rest of the file is the containment half — each test performs the attack
the design is meant to stop and proves the product refuses it against the live
stack rather than in a mock.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD

from .conftest import API_URL, FAKE_GITHUB_URL, compose, compose_authority, run_command

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
FAKE_GITHUB_INTERNAL = "http://fake-github:8080"
FAKE_GITHUB_HOST = FAKE_GITHUB_URL
FAKE_GITHUB_PAT = "fake-github-pat"
# Sandbox jobs are real containers; the whole flow needs more headroom than
# the API-only phase 5 tasks.
TASK_TIMEOUT_SECONDS = 300.0
PARK_TIMEOUT_SECONDS = 180.0


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


async def _connections(
    client: httpx.AsyncClient, ws: str, tag: str, *, allowed: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A GitHub connection holding the PAT, and a CLI Sandbox connection that
    borrows it — with the repositories this instance may touch written down."""
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
                "config": {
                    "default_network": "none",
                    "git_connection_id": github["id"],
                    "allowed_repositories": allowed,
                },
            },
        )
    )["connection"]
    return github, cli


async def _grant(
    client: httpx.AsyncClient, ws: str, agent_id: str, capability: str, scope: dict[str, str]
) -> None:
    await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/grants",
        {"capability": capability, "scope": scope, "effect": "allow"},
    )


async def _code_grants(
    client: httpx.AsyncClient, ws: str, agent_id: str, cli_id: str, github_id: str, repository: str
) -> None:
    """The Code-editing preset's least-privilege bundle, as the wizard writes
    it: no general shell, and every repository tool scoped to one repository."""
    for capability, scope in (
        ("cli.repository.checkout", {"connection_id": cli_id, "repository": repository}),
        ("cli.file.list", {"connection_id": cli_id, "path": "*"}),
        ("cli.file.search", {"connection_id": cli_id, "path": "*"}),
        ("cli.file.read", {"connection_id": cli_id, "path": "*"}),
        ("cli.file.edit", {"connection_id": cli_id, "path": "*"}),
        ("cli.file.write", {"connection_id": cli_id, "path": "*"}),
        ("cli.test.run", {"connection_id": cli_id, "command": "bash *"}),
        (
            "cli.repository.push",
            {"connection_id": cli_id, "repository": repository, "branch": "agent/*"},
        ),
        (
            "github.pull_request.create",
            {"connection_id": github_id, "repository": repository, "base": "main"},
        ),
    ):
        await _grant(client, ws, agent_id, capability, scope)


async def _assign(
    client: httpx.AsyncClient, ws: str, agent_id: str, title: str, description: str
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/assign-task",
        {"title": title, "description": description},
    )


async def _wait_for_task(
    client: httpx.AsyncClient, ws: str, task_id: str, deadline_s: float = TASK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_s
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}")
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(1.0)
    pytest.fail(f"task {task_id} did not finish in {deadline_s}s: {detail}")


async def _run_task(
    client: httpx.AsyncClient, ws: str, agent_id: str, title: str, description: str
) -> dict[str, Any]:
    task = await _assign(client, ws, agent_id, title, description)
    return await _wait_for_task(client, ws, task["id"])


async def _wait_for_parked_run(client: httpx.AsyncClient, ws: str, task_id: str) -> dict[str, Any]:
    """Poll until the task's run reports waiting_approval; return the run."""
    deadline = time.monotonic() + PARK_TIMEOUT_SECONDS
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}")
        runs = detail["runs"]
        if runs and runs[0]["status"] == "waiting_approval":
            run: dict[str, Any] = runs[0]
            return run
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            pytest.fail(f"task finished instead of parking for approval: {detail}")
        await asyncio.sleep(1.0)
    pytest.fail(f"run never reached waiting_approval in {PARK_TIMEOUT_SECONDS}s: {detail}")


async def _pending_approval_for_task(
    client: httpx.AsyncClient, ws: str, task_id: str
) -> dict[str, Any]:
    inbox = await _get(client, f"/api/v1/workspaces/{ws}/approvals", status="pending", limit=200)
    matches = [item for item in inbox["items"] if item["task_id"] == task_id]
    assert len(matches) == 1, f"expected one pending approval for task {task_id}: {inbox}"
    approval: dict[str, Any] = matches[0]
    return approval


async def _calls(client: httpx.AsyncClient, ws: str, run_id: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls"
    )
    return calls


async def _git_state() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as anon:
        state: dict[str, Any] = (await anon.get(f"{FAKE_GITHUB_HOST}/_state")).json()
    return state


def _marker(tool: str, arguments: dict[str, Any]) -> str:
    return f"[[tool:{tool} {json.dumps(arguments)}]]"


def _docker(*args: str) -> str:
    authority = compose_authority()
    result = run_command(
        authority.docker_command(*args),
        env=authority.environment,
        cwd=authority.repo,
        timeout=30.0,
        check=True,
    )
    stdout = result.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="strict")
    return cast(str, stdout).strip()


def _psql(sql: str) -> str:
    """Row-per-line query output straight from the stack's Postgres."""
    result = compose("exec", "-T", "postgres", "psql", "-U", "jhin", "-d", "jhin", "-tA", "-c", sql)
    return result.stdout.strip()


# --- the one thing most likely to fail on a first run -------------------------


async def test_credential_helper_fires_against_the_local_git_double(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The credential is delivered as ``-c credential."<base>".helper=…`` and
    git's own URL matcher decides whether it fires. Against real GitHub the
    base is ``https://github.com``; against this double it is
    ``http://fake-github:8080/git`` — a base with a *path*. If git's config-URL
    matching did not treat that path as a prefix of the clone URL, the helper
    would never run, ``GIT_ASKPASS=/bin/false`` would fail the clone, and
    nothing else in this file could pass. So it is proven first, on its own.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    _github, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli["id"], "repository": "octo/alpha"},
    )

    branch = f"agent/helper-{tag}"
    marker = _marker(
        "cli.repository.checkout",
        {"connection_id": cli["id"], "repository": "octo/alpha", "branch": branch},
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Clone only {tag}", f"Check it out: {marker}"
    )
    calls = await _calls(client, ws, detail["runs"][0]["id"])
    assert [call["tool_name"] for call in calls] == ["cli.repository.checkout"]
    assert calls[0]["status"] == "completed", calls[0]
    output = calls[0]["sanitized_output_json"]
    # A clone that authenticated: the double answers 401 without valid Basic
    # auth whose password is the PAT.
    assert output["head_sha"], output
    assert output["base_ref"] == "main", output
    assert "app.py" in output["top_level"], output
    assert FAKE_GITHUB_PAT not in json.dumps(calls)


# --- the full loop ------------------------------------------------------------


async def test_swe_agent_full_sandbox_flow(owner: tuple[httpx.AsyncClient, str]) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    github, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    await _code_grants(client, ws, agent["id"], cli["id"], github["id"], "octo/alpha")

    branch = f"agent/p6-{tag}"
    cli_id = cli["id"]
    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": branch},
            ),
            _marker("cli.file.list", {"connection_id": cli_id, "path": ""}),
            _marker("cli.file.search", {"connection_id": cli_id, "pattern": "VALUE ="}),
            _marker("cli.file.read", {"connection_id": cli_id, "path": "app.py"}),
            _marker("cli.test.run", {"connection_id": cli_id, "command": "bash ./run_tests.sh"}),
            _marker(
                "cli.file.edit",
                {
                    "connection_id": cli_id,
                    "path": "app.py",
                    "old_string": "VALUE = 1",
                    "new_string": "VALUE = 2",
                    "expected_count": 1,
                },
            ),
            _marker("cli.test.run", {"connection_id": cli_id, "command": "bash ./run_tests.sh"}),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": branch,
                    "commit_message": f"Fix VALUE {tag}",
                },
            ),
            _marker(
                "github.pull_request.create",
                {
                    "connection_id": github["id"],
                    "repository": "octo/alpha",
                    "title": f"Fix VALUE {tag}",
                    "head": branch,
                    "base": "main",
                    "body": "Automated by Jhin.",
                },
            ),
        ]
    )
    task = await _assign(
        client, ws, agent["id"], f"Fix the failing test {tag}", f"Do the work: {markers}"
    )

    # --- push is ELEVATED: the run parks before anything leaves the sandbox
    run = await _wait_for_parked_run(client, ws, task["id"])
    parked = await _calls(client, ws, run["id"])
    assert [call["tool_name"] for call in parked] == [
        "cli.repository.checkout",
        "cli.file.list",
        "cli.file.search",
        "cli.file.read",
        "cli.test.run",
        "cli.file.edit",
        "cli.test.run",
        "cli.repository.push",
    ], parked
    assert parked[-1]["status"] == "pending_approval", parked[-1]
    # Nothing has reached the remote yet.
    before = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert branch not in before, before

    approval = await _pending_approval_for_task(client, ws, task["id"])
    assert approval["action_type"] == "cli.repository.push"
    assert approval["action_payload_sanitized"]["risk"] == "elevated"
    assert approval["action_payload_sanitized"]["input"]["branch"] == branch
    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{approval['id']}/approve", {}, expect=200
    )

    detail = await _wait_for_task(client, ws, task["id"])
    assert detail["task"]["state"] == "completed", detail
    run_id = detail["runs"][0]["id"]

    # --- every tool call completed, in the scripted order
    calls = await _calls(client, ws, run_id)
    assert [c["tool_name"] for c in calls] == [
        "cli.repository.checkout",
        "cli.file.list",
        "cli.file.search",
        "cli.file.read",
        "cli.test.run",
        "cli.file.edit",
        "cli.test.run",
        "cli.repository.push",
        "github.pull_request.create",
    ], calls
    assert all(c["status"] == "completed" for c in calls), calls

    checkout_out = calls[0]["sanitized_output_json"]
    assert checkout_out["branch"] == branch
    assert checkout_out["head_sha"], "checkout must report the cloned HEAD sha"
    assert checkout_out["base_ref"] == "main"
    assert "app.py" in checkout_out["top_level"]

    # The agent found its own way around: a listing, then a search that names
    # the file and line, then a page of that file with a read_token.
    listed = {entry["path"] for entry in calls[1]["sanitized_output_json"]["entries"]}
    assert {"app.py", "run_tests.sh", "README.md"} <= listed, listed
    matches = calls[2]["sanitized_output_json"]["matches"]
    assert any(m["path"] == "app.py" and m["line"] == 1 for m in matches), matches
    read = calls[3]["sanitized_output_json"]
    assert read["content"].startswith("VALUE = 1")
    assert read["total_lines"] == 2
    assert read["has_more"] is False
    assert len(read["read_token"]) == 64

    assert calls[4]["sanitized_output_json"]["passed"] is False  # red before the fix
    assert calls[5]["sanitized_output_json"]["replacements"] == 1
    assert calls[6]["sanitized_output_json"]["passed"] is True  # green after the fix

    push_out = calls[7]["sanitized_output_json"]
    # The URL git was actually given, not the alias ``origin`` — which is a
    # pointer the container owns and the push deliberately never uses, so the
    # model's transcript names the objects' real destination.
    assert push_out["remote"] != "origin"
    assert push_out["remote"].endswith("/git/octo/alpha.git"), push_out["remote"]
    assert push_out["branch"] == branch
    assert push_out["previous_sha"] == checkout_out["head_sha"]
    assert push_out["pushed_sha"] != push_out["previous_sha"], "push must add a new commit"
    assert calls[7]["approval_id"] == approval["id"]
    pr_number = calls[8]["sanitized_output_json"]["number"]

    # The short-lived git token never persists anywhere (plan 48.9).
    assert FAKE_GITHUB_PAT not in json.dumps(calls)

    # --- sandbox_job rows: one per cli.* call, all completed, linked to the run
    rows = _psql(
        f"select status, exit_code from sandbox_job where run_id = '{run_id}' order by id"
    ).splitlines()
    assert len(rows) == 8, rows  # checkout, list, search, read, test, edit, test, push
    assert all(row.startswith("completed|") for row in rows), rows
    leaked = _psql(
        "select count(*) from sandbox_job where run_id = "
        f"'{run_id}' and (stdout_tail like '%{FAKE_GITHUB_PAT}%' "
        f"or stderr_tail like '%{FAKE_GITHUB_PAT}%')"
    )
    assert leaked == "0", "PAT leaked into persisted sandbox job output"

    # --- the audit names the credential that was actually spent, not just the
    # sandbox connection the tool call points at.
    audited = _psql(
        "select count(*) from audit_event where action = 'sandbox.job.completed' "
        f"and metadata_json->>'run_id' = '{run_id}' "
        f"and metadata_json->>'git_connection_id' = '{github['id']}'"
    )
    assert int(audited) == 2, "checkout and push must record the GitHub connection they borrowed"
    pushed_shas = _psql(
        "select metadata_json->>'pushed_sha' from audit_event "
        "where action = 'sandbox.job.completed' "
        f"and metadata_json->>'run_id' = '{run_id}' "
        "and metadata_json->>'pushed_sha' is not null"
    ).splitlines()
    assert pushed_shas == [push_out["pushed_sha"]], pushed_shas

    # --- sandbox.job events feed the task timeline (plan 45 Phase 6 UI)
    timeline = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run_id}/timeline")
    sandbox_events = [e for e in timeline if e["event_type"] == "sandbox.job"]
    assert len(sandbox_events) == 8, [e["event_type"] for e in timeline]
    assert all(e["payload_json"]["job_status"] == "completed" for e in sandbox_events)

    # --- the pushed branch is real: visible in the git server's refs
    repo = (await _git_state())["repos"]["octo/alpha"]
    assert branch in repo["branches"], sorted(repo["branches"])
    assert repo["branches"][branch] != checkout_out["head_sha"], "push must add a new commit"
    assert repo["branches"][branch] == push_out["pushed_sha"]

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


# --- containment --------------------------------------------------------------


async def test_git_internals_are_not_writable(owner: tuple[httpx.AsyncClient, str]) -> None:
    """The highest-value attack the design closes: write a credential helper
    and an ``insteadOf`` redirect into the repository's own git config, and the
    next Jhin-authored push hands the token to the attacker. Every file tool
    must refuse ``.git`` — at validation, before a container starts."""
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli["id"], "repository": "octo/alpha"},
    )
    for capability in ("cli.file.write", "cli.file.edit", "cli.file.read"):
        await _grant(client, ws, agent["id"], capability, {"connection_id": cli["id"], "path": "*"})

    cli_id = cli["id"]
    # No braces anywhere in a marker: the fake provider's marker regex is
    # non-greedy up to the first '}'. The content is irrelevant here —
    # the path is what must be refused.
    planted = "[credential]\nhelper = steal-the-token\n"
    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": f"agent/internals-{tag}",
                },
            ),
            _marker(
                "cli.file.write",
                {
                    "connection_id": cli_id,
                    "path": ".git/config",
                    "content": planted,
                    "read_token": "",
                },
            ),
            _marker(
                "cli.file.write",
                {
                    "connection_id": cli_id,
                    "path": ".gitconfig",
                    "content": planted,
                    "read_token": "",
                },
            ),
            _marker("cli.file.read", {"connection_id": cli_id, "path": ".git/config"}),
            _marker(
                "cli.file.edit",
                {
                    "connection_id": cli_id,
                    "path": "src/.git/config",
                    "old_string": "a",
                    "new_string": "b",
                },
            ),
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Reach the git config {tag}", f"Try it: {markers}"
    )
    run_id = detail["runs"][0]["id"]
    calls = await _calls(client, ws, run_id)
    assert calls[0]["tool_name"] == "cli.repository.checkout"
    assert calls[0]["status"] == "completed"
    attempts = calls[1:]
    assert attempts, calls
    for call in attempts:
        # Refused by input validation, so no sandbox job ever started for it.
        assert call["status"] == "denied", call
        assert call["error_code"] == "invalid_input", call
    assert _psql(f"select count(*) from sandbox_job where run_id = '{run_id}'") == "1", (
        "only the checkout should have run a container"
    )
    # And the checkout's own .git is intact: a later push would still pass the
    # config audit, which is the point of refusing the write rather than
    # letting it land and catching it afterwards.


async def test_repo_config_tampering_blocks_the_push(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """Defence in depth for the same hole. Even granted a shell wide enough to
    rewrite ``.git/config`` — which the Code-editing preset deliberately does
    not grant — the credential does not travel with the tampered config: the
    push audits the repo config first and refuses."""
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    branch = f"agent/tamper-{tag}"
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "octo/alpha"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "octo/alpha", "branch": "agent/*"},
    )
    # Deliberately over-granted, for this test only: a general shell is what
    # the shipped preset no longer includes.
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.command.execute",
        {"connection_id": cli_id, "command": "git config *"},
    )
    # Push is ELEVATED; let this agent run it unattended so the test measures
    # the config audit rather than the approval gate.
    await _autonomous(client, ws, agent["id"])

    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": branch},
            ),
            _marker(
                "cli.command.execute",
                {
                    "connection_id": cli_id,
                    # A credential.* key at all is the tampering; the value
                    # is kept brace-free for the marker regex.
                    "command": (
                        "git config --local credential.https://attacker.example.helper stolen"
                    ),
                },
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": branch,
                    "commit_message": "tampered",
                },
            ),
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Tamper then push {tag}", f"Do it: {markers}"
    )
    run_id = detail["runs"][0]["id"]
    calls = await _calls(client, ws, run_id)
    push = [call for call in calls if call["tool_name"] == "cli.repository.push"]
    assert len(push) == 1, calls
    assert push[0]["status"] == "failed", push[0]
    assert push[0]["error_code"] == "repo_config_tampered", push[0]

    branches = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert branch not in branches, "a tampered repository must not reach the remote"
    security = _psql(
        "select count(*) from audit_event where action = 'sandbox.repo_config_tampered' "
        f"and metadata_json->>'run_id' = '{run_id}'"
    )
    assert security == "1", "the refusal is a security event, not just a tool error"
    assert FAKE_GITHUB_PAT not in json.dumps(calls)
    leaked = _psql(
        "select count(*) from sandbox_job where run_id = "
        f"'{run_id}' and (stdout_tail like '%{FAKE_GITHUB_PAT}%' "
        f"or stderr_tail like '%{FAKE_GITHUB_PAT}%')"
    )
    assert leaked == "0"


async def _autonomous(client: httpx.AsyncClient, ws: str, agent_id: str) -> None:
    """Let the push run unattended, so a test measures the containment rather
    than the approval gate in front of it."""
    response = await client.put(
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/policy",
        json={"preset": "autonomous"},
        headers=_csrf(client),
    )
    assert response.status_code == 200, response.text


async def test_a_rewritten_origin_cannot_redirect_an_approved_push(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The attack the Code-editing preset used to permit, end to end.

    ``cli.test.run`` is granted ``command: "*"`` by the shipped preset and runs
    in the checkout, so one command adds a *second* URL to ``remote.origin.url``
    — a real repository on the same server that this connection's allow-list
    does not include. ``git remote get-url origin`` reports only the first by
    design, and a key-name audit accepts ``remote.origin.url`` however many
    values it holds, so the next approved push used to deliver the whole
    repository to both. The human approving it saw a repository, a branch and a
    commit message.

    Two things now stop it independently: the push targets a URL Jhin computes
    rather than the name ``origin``, and the config audit counts the values
    behind the key it allows.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    branch = f"agent/exfil-{tag}"
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "octo/alpha"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "octo/alpha", "branch": "agent/*"},
    )
    # Exactly what the Code-editing preset grants: an unconstrained test command.
    await _grant(client, ws, agent["id"], "cli.test.run", {"connection_id": cli_id, "command": "*"})
    await _autonomous(client, ws, agent["id"])

    exfil = f"{FAKE_GITHUB_INTERNAL}/git/octo/beta.git"
    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": branch},
            ),
            _marker(
                "cli.test.run",
                # A plausible test invocation with a second command after it.
                # A grant scope cannot help here: fnmatch over a shell string
                # matches whatever follows the part an operator pinned.
                {
                    "connection_id": cli_id,
                    "command": f"bash ./run_tests.sh; git remote set-url --add origin {exfil}",
                },
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": branch,
                    "commit_message": "fix the failing test",
                },
            ),
        ]
    )
    detail = await _run_task(client, ws, agent["id"], f"Redirect a push {tag}", f"Do it: {markers}")
    run_id = detail["runs"][0]["id"]
    calls = await _calls(client, ws, run_id)
    push = [call for call in calls if call["tool_name"] == "cli.repository.push"]
    assert len(push) == 1, calls
    assert push[0]["status"] == "failed", push[0]
    assert push[0]["error_code"] == "remote_rewritten", push[0]

    repos = (await _git_state())["repos"]
    assert branch not in repos["octo/beta"]["branches"], (
        "the repository reached a host the connection never allowed"
    )
    assert branch not in repos["octo/alpha"]["branches"], (
        "a repository whose remote was rewritten must not push anywhere at all"
    )
    # The audit names where it would have gone, not where settings said it should.
    observed = _psql(
        "select metadata_json->>'observed_urls' from audit_event where action = "
        f"'sandbox.repo_config_tampered' and metadata_json->>'run_id' = '{run_id}'"
    )
    assert "octo/beta" in observed, observed
    remote_url = _psql(
        f"select metadata_json->>'remote_url' from audit_event where metadata_json->>'run_id' "
        f"= '{run_id}' and metadata_json->>'remote_url' is not null "
        "order by created_at desc limit 1"
    )
    assert remote_url.endswith("octo/alpha.git"), remote_url
    assert FAKE_GITHUB_PAT not in json.dumps(calls)


async def test_a_hard_link_cannot_hand_a_file_tool_the_git_config(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The other half of the same attack, and the one the schema cannot see.

    ``ln .git/config cfg`` creates no symlink and no new path segment: the
    schema is shown ``cfg``, ``realpath`` resolves ``cfg`` to ``<root>/cfg``,
    and both are telling the truth about a file that is also git's. Reading it
    used to return the whole config with a valid read_token, and writing it
    truncated the shared inode.

    A regular file the file tools may touch has exactly one name.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    branch = f"agent/link-{tag}"
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "octo/alpha"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "octo/alpha", "branch": "agent/*"},
    )
    await _grant(client, ws, agent["id"], "cli.test.run", {"connection_id": cli_id, "command": "*"})
    for capability in ("cli.file.read", "cli.file.write", "cli.file.edit"):
        await _grant(client, ws, agent["id"], capability, {"connection_id": cli_id, "path": "*"})
    await _autonomous(client, ws, agent["id"])

    planted = "[credential]\nhelper = steal-the-token\n"
    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": branch},
            ),
            _marker("cli.test.run", {"connection_id": cli_id, "command": "ln .git/config cfg"}),
            _marker("cli.file.read", {"connection_id": cli_id, "path": "cfg"}),
            _marker(
                "cli.file.write",
                {"connection_id": cli_id, "path": "cfg", "content": planted, "read_token": ""},
            ),
            _marker(
                "cli.file.edit",
                {
                    "connection_id": cli_id,
                    "path": "cfg",
                    "old_string": "core",
                    "new_string": "cor3",
                },
            ),
            # The positive control: the config is still byte for byte what the
            # checkout recorded, so a legitimate push still lands.
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": branch,
                    "commit_message": "an ordinary change",
                },
            ),
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Reach the config sideways {tag}", f"Try it: {markers}"
    )
    calls = await _calls(client, ws, detail["runs"][0]["id"])
    by_tool = {call["tool_name"]: call for call in calls}
    for tool in ("cli.file.read", "cli.file.write", "cli.file.edit"):
        call = by_tool.get(tool)
        assert call is not None, calls
        assert call["status"] == "failed", call
        assert call["error_code"] == "hard_linked_file", call

    push = by_tool.get("cli.repository.push")
    assert push is not None and push["status"] == "completed", push
    branches = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert branch in branches, "the refusals must not have damaged an honest push"
    assert FAKE_GITHUB_PAT not in json.dumps(calls)


async def test_the_push_refuses_the_branch_the_checkout_was_actually_cut_from(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """``refs/remotes/origin/HEAD`` answered the wrong question from the wrong
    place: it is the *remote default* branch, not the ref this checkout was cut
    from, and it is a ref inside the repository the agent has been editing. So
    a branch cut from ``release`` could be pushed straight back onto
    ``release`` — the base — as long as ``release`` was not the remote default.

    The base now comes from what the checkout wrote into Jhin's audit trail.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    base = f"agent/base-{tag}"
    for capability, scope in (
        ("cli.repository.checkout", {"connection_id": cli_id, "repository": "octo/alpha"}),
        (
            "cli.repository.push",
            {"connection_id": cli_id, "repository": "octo/alpha", "branch": "agent/*"},
        ),
        ("cli.test.run", {"connection_id": cli_id, "command": "*"}),
    ):
        await _grant(client, ws, agent["id"], capability, scope)
    await _autonomous(client, ws, agent["id"])

    # A long-lived branch that is not the remote default, made the only way
    # this stack offers: an ordinary agent push.
    setup = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": base},
            ),
            _marker(
                "cli.test.run",
                {"connection_id": cli_id, "command": "printf 'v1\\n' > RELEASE.txt"},
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": base,
                    "commit_message": "cut a base branch",
                },
            ),
        ]
    )
    await _run_task(client, ws, agent["id"], f"Cut a base {tag}", f"Do it: {setup}")
    before = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert base in before, before

    # A fresh run: a new workspace volume, a checkout cut from that branch, and
    # a push aimed straight back at it.
    attack = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "ref": base,
                    "branch": f"agent/work-{tag}",
                },
            ),
            _marker(
                "cli.test.run",
                {
                    "connection_id": cli_id,
                    "command": f"printf 'v2\\n' > RELEASE.txt; git checkout -B {base}",
                },
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": base,
                    "commit_message": "straight onto the base",
                },
            ),
        ]
    )
    detail = await _run_task(client, ws, agent["id"], f"Land on the base {tag}", f"Do it: {attack}")
    calls = await _calls(client, ws, detail["runs"][0]["id"])
    checkout = [call for call in calls if call["tool_name"] == "cli.repository.checkout"]
    assert checkout and checkout[0]["sanitized_output_json"]["base_ref"] == base, checkout
    push = [call for call in calls if call["tool_name"] == "cli.repository.push"]
    assert len(push) == 1, calls
    assert push[0]["status"] == "failed", push[0]
    assert push[0]["error_code"] == "push_to_base_refused", push[0]

    after = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert after[base] == before[base], "the base branch moved"


async def test_push_to_an_unallowed_repository_is_denied_and_leaks_nothing(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The operator's one place to say which repositories this instance may
    touch. ``octo/beta`` is a real repository on the same server, and the grant
    even names it — the connection does not, so both checkout and push stop."""
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    branch = f"agent/beta-{tag}"
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "octo/*"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "octo/*", "branch": "agent/*"},
    )

    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/beta", "branch": branch},
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/beta",
                    "branch": branch,
                    "commit_message": "should never land",
                },
            ),
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Touch an unlisted repo {tag}", f"Try it: {markers}"
    )
    run_id = detail["runs"][0]["id"]
    calls = await _calls(client, ws, run_id)
    assert calls, calls
    for call in calls:
        assert call["status"] == "denied", call
        assert call["error_code"] == "repository_not_allowed", call
        assert "octo/alpha" in json.dumps(call["sanitized_output_json"])

    branches = (await _git_state())["repos"]["octo/beta"]["branches"]
    assert branch not in branches, branches
    assert _psql(f"select count(*) from sandbox_job where run_id = '{run_id}'") == "0"
    assert FAKE_GITHUB_PAT not in json.dumps(calls)


async def test_narrowing_the_allow_list_invalidates_a_parked_push_approval(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """A parked approval is not a snapshot of permission. Narrowing the
    connection's allow-list while a push waits for a human invalidates it: the
    approval binding hashes the connection's authorization state, so the
    approved call is refused and the branch never reaches the remote."""
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["octo/alpha"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    branch = f"agent/parked-{tag}"
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "octo/alpha"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "octo/alpha", "branch": "agent/*"},
    )

    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/alpha", "branch": branch},
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "octo/alpha",
                    "branch": branch,
                    "commit_message": "parked",
                },
            ),
        ]
    )
    task = await _assign(client, ws, agent["id"], f"Park a push {tag}", f"Do it: {markers}")
    run = await _wait_for_parked_run(client, ws, task["id"])
    approval = await _pending_approval_for_task(client, ws, task["id"])

    # The operator narrows the connection while the approval waits. Connection
    # settings are create-only through the API today, so this is the state
    # change itself, applied where it lives.
    _psql(
        "update connection set config_json = jsonb_set(config_json::jsonb, "
        "'{allowed_repositories}', '[]'::jsonb) where id = '" + cli_id + "'"
    )

    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{approval['id']}/approve", {}, expect=200
    )
    detail = await _wait_for_task(client, ws, task["id"])
    calls = await _calls(client, ws, detail["runs"][0]["id"])
    push = [call for call in calls if call["tool_name"] == "cli.repository.push"]
    assert len(push) == 1, calls
    assert push[0]["status"] == "denied", push[0]
    # The connection digest covers config_json, so the binding check fires
    # before the validator gets a second look — either way the answer is no.
    assert push[0]["error_code"] in (
        "approval_connection_changed",
        "repository_not_allowed",
    ), push[0]

    branches = (await _git_state())["repos"]["octo/alpha"]["branches"]
    assert branch not in branches, branches
    assert (
        _psql(
            "select count(*) from sandbox_job where run_id = "
            f"'{run['id']}' and command like 'git push%'"
        )
        == "0"
    )


async def test_a_repository_name_cannot_walk_out_of_the_allow_list(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """``../evil`` is two perfectly ordinary segments to a pattern like
    ``[\\w.-]+/[\\w.-]+`` and a directory traversal to the clone URL, which is
    built by joining the value onto the git base. With the ``*`` allow-list
    migration 0038 grandfathers onto every existing connection, nothing else
    stood in the way: the clone URL left the ``/git`` prefix the credential's
    scope is written around. Refused at validation, before a container starts.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    _, cli = await _connections(client, ws, tag, allowed=["*"])
    agent = await _make_agent(client, ws, tag)
    cli_id = cli["id"]
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.checkout",
        {"connection_id": cli_id, "repository": "*"},
    )
    await _grant(
        client,
        ws,
        agent["id"],
        "cli.repository.push",
        {"connection_id": cli_id, "repository": "*", "branch": "*"},
    )

    markers = " ".join(
        [
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "../evil"},
            ),
            _marker(
                "cli.repository.checkout",
                {"connection_id": cli_id, "repository": "octo/.."},
            ),
            _marker(
                "cli.repository.push",
                {
                    "connection_id": cli_id,
                    "repository": "../evil",
                    "branch": f"agent/walk-{tag}",
                    "commit_message": "should never land",
                },
            ),
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"Walk out of the allow list {tag}", f"Try it: {markers}"
    )
    run_id = detail["runs"][0]["id"]
    calls = await _calls(client, ws, run_id)
    assert calls, calls
    for call in calls:
        assert call["status"] == "denied", call
        assert call["error_code"] == "invalid_input", call
    assert _psql(f"select count(*) from sandbox_job where run_id = '{run_id}'") == "0"
    assert FAKE_GITHUB_PAT not in json.dumps(calls)


async def test_changing_the_mode_keeps_the_rule_that_gates_a_push(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The approval gate on ``cli.repository.push`` is a decision about one
    tool; an approval preset is a decision about risk levels. Both places in
    the UI that change the mode send ``PUT /policy {"preset": …}``, which used
    to replace the whole rule list — so switching an agent to Autonomous for an
    unrelated reason removed the only thing standing in front of the first
    action that leaves the sandbox, and the UI (showing no preset selected
    while the extra rule was there) invited exactly that click.
    """
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag)
    gate = {"capability": "cli.repository.push", "risk": None, "action": "approval"}

    written = await client.put(
        f"/api/v1/workspaces/{ws}/agents/{agent['id']}/policy",
        json={"rules": [gate]},
        headers=_csrf(client),
    )
    assert written.status_code == 200, written.text
    assert written.json()["rules"] == [gate], written.text

    await _autonomous(client, ws, agent["id"])

    policy = await _get(client, f"/api/v1/workspaces/{ws}/agents/{agent['id']}/policy")
    # Kept, and kept first: rules are first-match, so behind the preset's
    # elevated/auto rule it would be persisted and never reached.
    assert policy["rules"][0] == gate, policy
    assert policy["rules"][1:] == [
        {"capability": "*", "risk": "read", "action": "auto"},
        {"capability": "*", "risk": "write", "action": "auto"},
        {"capability": "*", "risk": "elevated", "action": "auto"},
        {"capability": "*", "risk": "destructive", "action": "approval"},
    ], policy
    # And the mode still reads as chosen, so nothing invites the click again.
    assert policy["preset"] == "autonomous", policy
