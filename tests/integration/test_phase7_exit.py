"""Phase 7 exit tests (plan 45 + 26): the flagship vertical slice.

A Linear issue transitioning into Todo automatically starts exactly one SWE
task that works in a sandbox and opens a GitHub PR — with every dedupe layer
proven:

(a) THE SLICE — ENG-142 moves Backlog → Todo in fake Linear, which fires a
    properly-signed webhook at the API. Exactly one trigger invocation, one
    task (linked external_source/external_id), one TriggeredTaskWorkflow;
    the SWE run does checkout → fix → test → push → PR in the sandbox; the
    timeline shows the chain; comment-back lands in fake Linear.
(b) DUPLICATE DELIVERY — byte-for-byte redelivery (same delivery id) is
    absorbed by webhook delivery dedupe; a semantically identical second
    event (same transition, new delivery id) is absorbed by the trigger
    idempotency key. Still exactly one task.
(c) NO-TRANSITION — a title edit while the issue sits in Todo (updatedFrom
    has no state change) does not invoke the trigger.
(d) FILTER MISS — a Todo transition on a team the filter doesn't match
    never invokes, while a control trigger on the right team does.
(e) TEST ENDPOINT — dry-run evaluation returns per-condition explanations.

Isolation: each test creates its own Linear connection, so the fake's single
webhook target points at that connection; the TriggerMatcher only matches
triggers bound to the event's connection, so seeded/showcase triggers never
interfere with these tests (and vice versa).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD

from .conftest import API_URL, FAKE_GITHUB_URL, FAKE_LINEAR_URL, compose

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
FAKE_GITHUB_INTERNAL = "http://fake-github:8080"
FAKE_GITHUB_HOST = FAKE_GITHUB_URL
FAKE_GITHUB_PAT = "fake-github-pat"
FAKE_LINEAR_INTERNAL = "http://fake-linear:8080"
FAKE_LINEAR_HOST = FAKE_LINEAR_URL
FAKE_LINEAR_API_KEY = "fake-linear-api-key"
# fake-linear POSTs webhooks at the API over the compose "data" network.
API_INTERNAL_URL = "http://api:8000"

TASK_TIMEOUT_SECONDS = 300.0
# Trigger cache TTL is 5s and NATS → normalize → match is async; be generous.
INVOCATION_TIMEOUT_SECONDS = 60.0
# How long we wait to prove something did NOT happen.
QUIET_SECONDS = 8.0


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


async def _put(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.put(path, json=body, headers=_csrf(client))
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


# --- fake Linear driving ---


async def _fake_admin(path: str, body: dict[str, Any], expect: int = 200) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as anon:
        response = await anon.post(f"{FAKE_LINEAR_HOST}{path}", json=body)
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _fake_state() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as anon:
        response = await anon.get(f"{FAKE_LINEAR_HOST}/_state")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


async def _fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as anon:
        response = await anon.post(
            f"{FAKE_LINEAR_HOST}/graphql",
            json={"query": query, "variables": variables},
            headers={"Authorization": FAKE_LINEAR_API_KEY},
        )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    assert "errors" not in payload, payload
    data: dict[str, Any] = payload["data"]
    return data


async def _new_issue(title: str, description: str) -> str:
    """Create a fresh issue in team ENG (lands in Backlog); returns ENG-###."""
    state = await _fake_state()
    team_id = state["teams"]["ENG"]["id"]
    data = await _fake_graphql(
        "mutation issueCreate($input: IssueCreateInput!) { issueCreate(input: $input) "
        "{ success issue { identifier } } }",
        {"input": {"teamId": team_id, "title": title, "description": description}},
    )
    identifier: str = data["issueCreate"]["issue"]["identifier"]
    return identifier


async def _transition(identifier: str, state_name: str) -> dict[str, Any]:
    """Move the issue and fire the signed webhook; returns delivery info."""
    result = await _fake_admin(f"/_admin/issues/{identifier}/transition", {"state": state_name})
    assert result.get("delivered") is True, f"webhook not delivered: {result}"
    assert result.get("response") == 202, f"API rejected the webhook: {result}"
    return result


# --- API-side wiring ---


async def _linear_connection(
    client: httpx.AsyncClient, ws: str, tag: str
) -> tuple[dict[str, Any], str]:
    """Create a Linear connection and point fake-linear's webhook at it.
    Returns (connection, webhook secret)."""
    created = await _post(
        client,
        f"/api/v1/workspaces/{ws}/connections",
        {
            "connector_type": "linear",
            "name": f"P7 Linear {tag}",
            "auth_type": "api_key",
            "credentials": {"api_key": FAKE_LINEAR_API_KEY},
            "config": {"base_url": FAKE_LINEAR_INTERNAL},
        },
    )
    webhook = created["webhook"]
    assert webhook, "linear connections must return one-time webhook setup"
    await _fake_admin(
        "/_admin/webhook",
        {"url": f"{API_INTERNAL_URL}{webhook['url_path']}", "secret": webhook["secret"]},
    )
    return created["connection"], webhook["secret"]


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str) -> dict[str, Any]:
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P7 provider {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P7 profile {tag}",
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"P7 coder {tag}",
            "system_prompt": "You are a software engineer; use tools when instructed.",
            "model_profile_id": profile["id"],
        },
    )


async def _make_trigger(
    client: httpx.AsyncClient,
    ws: str,
    *,
    name: str,
    connection_id: str,
    agent_id: str,
    team_key: str = "ENG",
    comment_back: bool = False,
    dedupe_window_seconds: int = 3600,
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/triggers",
        {
            "name": name,
            "connection_id": connection_id,
            "event_type": "connector.linear.issue.updated",
            "filter": {
                "all": [
                    {"path": "data.team.key", "op": "eq", "value": team_key},
                    {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
                ]
            },
            "target_agent_id": agent_id,
            "action_config": {"comment_back": comment_back},
            "dedupe_window_seconds": dedupe_window_seconds,
        },
    )


async def _settle_trigger_cache() -> None:
    """The matcher caches a workspace's enabled triggers for 5s. Events fired
    within that window after creating a trigger could legitimately be
    evaluated against the stale set, so tests wait it out."""
    await asyncio.sleep(6.0)


async def _invocations(client: httpx.AsyncClient, ws: str, trigger_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/triggers/{trigger_id}/invocations"
    )
    return result


async def _wait_invocations(
    client: httpx.AsyncClient,
    ws: str,
    trigger_id: str,
    minimum: int,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + INVOCATION_TIMEOUT_SECONDS
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await _invocations(client, ws, trigger_id)
        if len(rows) >= minimum:
            return rows
        await asyncio.sleep(1.0)
    pytest.fail(f"trigger {trigger_id}: wanted ≥{minimum} invocations, got {rows}")


async def _wait_task_finished(client: httpx.AsyncClient, ws: str, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}")
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(1.0)
    pytest.fail(f"task {task_id} did not finish in {TASK_TIMEOUT_SECONDS}s: {detail}")


async def _tasks_for_agent(
    client: httpx.AsyncClient, ws: str, agent_id: str
) -> list[dict[str, Any]]:
    listing = await _get(client, f"/api/v1/workspaces/{ws}/tasks", agent_id=agent_id, limit=50)
    items: list[dict[str, Any]] = listing["items"]
    return items


# --- (a) THE SLICE ---


async def test_linear_todo_starts_swe_task_that_opens_pr(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]

    # Wiring: GitHub + CLI connections, an agent with sandbox grants (the
    # Phase 6 SWE toolbelt), a Linear connection, and the trigger.
    github = (
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/connections",
            {
                "connector_type": "github",
                "name": f"P7 GitHub {tag}",
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
                "name": f"P7 CLI {tag}",
                "auth_type": "none",
                "credentials": {},
                "config": {
                    "default_network": "none",
                    "git_connection_id": github["id"],
                    # The instance may only touch repositories written down
                    # here; a checkout of anything else is refused before a
                    # credential is minted.
                    "allowed_repositories": ["octo/alpha"],
                },
            },
        )
    )["connection"]
    agent = await _make_agent(client, ws, tag)
    branch = f"agent/p7-{tag}"
    grants = {
        "cli.repository.checkout": {"connection_id": cli["id"], "repository": "octo/alpha"},
        "cli.file.read": {"connection_id": cli["id"], "path": "*"},
        "cli.file.edit": {"connection_id": cli["id"], "path": "*"},
        "cli.test.run": {"connection_id": cli["id"], "command": "bash *"},
        # A push grant names the branches the agent may land on, and a pull
        # request grant names the base it may open against.
        "cli.repository.push": {
            "connection_id": cli["id"],
            "repository": "octo/alpha",
            "branch": "agent/*",
        },
        "github.pull_request.create": {
            "connection_id": github["id"],
            "repository": "octo/alpha",
            "base": "main",
        },
    }
    for capability, scope in grants.items():
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/grants",
            {"capability": capability, "scope": scope, "effect": "allow"},
        )
    # cli.repository.push is ELEVATED, so under the risk defaults this run
    # would park for a human and a trigger-started task would sit unattended
    # forever. Phase 6 owns that approval gate; the slice this file proves is
    # the unattended one, which is what an Autonomous agent is for.
    await _put(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent['id']}/policy",
        {"preset": "autonomous"},
    )
    linear, _secret = await _linear_connection(client, ws, tag)
    trigger = await _make_trigger(
        client,
        ws,
        name=f"P7 slice {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
        comment_back=True,
    )

    # ENG-142's fix instructions, in fake-provider marker form so the model
    # replays the exact Phase 6 SWE flow (checkout → red → fix → green →
    # push → PR).
    cli_id = cli["id"]
    markers = " ".join(
        [
            f'[[tool:cli.repository.checkout {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "branch": "{branch}"}}]]',
            f'[[tool:cli.file.read {{"connection_id": "{cli_id}", "path": "app.py"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:cli.file.edit {{"connection_id": "{cli_id}", "path": "app.py", '
            f'"old_string": "VALUE = 1", "new_string": "VALUE = 2", "expected_count": 1}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            # The branch leaves the sandbox through the tool that holds the
            # credential. A model-authored shell has no git credential at all,
            # so a scripted "git push" here would only prove the refusal.
            f'[[tool:cli.repository.push {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "branch": "{branch}", '
            f'"commit_message": "Fix VALUE {tag}"}}]]',
            f'[[tool:github.pull_request.create {{"connection_id": "{github["id"]}", '
            f'"repository": "octo/alpha", "title": "Fix VALUE {tag}", '
            f'"head": "{branch}", "base": "main", "body": "Automated by Jhin."}}]]',
        ]
    )

    # The plan's fixture issue is ENG-142; make the run repeatable: reset it
    # to Backlog if a previous run left it in Todo (that transition doesn't
    # match the filter), then plant the scripted fix instructions.
    state = await _fake_state()
    eng142_state_id = state["issues"]["ENG-142"]["stateId"]
    states = {s["id"]: s["name"] for s in state["teams"]["ENG"]["states"]}
    if states[eng142_state_id] != "Backlog":
        await _transition("ENG-142", "Backlog")
    await _fake_admin("/_admin/issues/ENG-142/edit", {"description": f"Do the work: {markers}"})
    await _settle_trigger_cache()

    # --- the moment: ENG-142 Backlog → Todo ---
    delivery = await _transition("ENG-142", "Todo")
    assert delivery["delivery_id"]

    # Exactly one invocation, status started, with task + workflow linked.
    rows = await _wait_invocations(client, ws, trigger["id"], minimum=1)
    assert len(rows) == 1, rows
    invocation = rows[0]
    assert invocation["status"] == "started", invocation
    assert invocation["workflow_id"], invocation

    # The task exists, linked to Linear, and completes the full SWE flow.
    deadline = time.monotonic() + INVOCATION_TIMEOUT_SECONDS
    while invocation["task_id"] is None and time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        invocation = (await _invocations(client, ws, trigger["id"]))[0]
    assert invocation["task_id"], "invocation never linked a task"

    detail = await _wait_task_finished(client, ws, invocation["task_id"])
    task = detail["task"]
    assert task["state"] == "completed", detail
    assert task["external_source"] == "linear"
    assert task["external_id"] == "ENG-142"
    assert task["trigger_id"] == trigger["id"]
    assert task["title"].startswith("[ENG-142]")
    assert task["metadata_json"]["trigger_name"] == f"P7 slice {tag}"
    assert "linear.fake" in task["metadata_json"]["external_url"]

    # Exactly one task for this agent — the trigger started it, nothing else.
    tasks = await _tasks_for_agent(client, ws, agent["id"])
    assert len(tasks) == 1, tasks

    # Timeline chain: trigger origin recorded as a system message, then the
    # run with every scripted tool call followed by the durable sync claim.
    messages = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}/messages")
    origin = [
        m
        for m in messages
        if m["sender_type"] == "system"
        and "Started by trigger" in str(m["content_json"].get("text", ""))
    ]
    assert origin, messages
    origin_text = origin[0]["content_json"]["text"]
    assert f"P7 slice {tag}" in origin_text
    assert "ENG-142" in origin_text

    run_id = detail["runs"][0]["id"]
    deadline = time.monotonic() + 30.0
    calls: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        calls = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls")
        sync_completed = bool(calls) and calls[-1]["tool_name"] == "system.trigger.sync_external"
        sync_completed = sync_completed and calls[-1]["status"] == "completed"
        if sync_completed:
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail(f"trigger sync claim did not complete: {calls}")
    assert [c["tool_name"] for c in calls] == [
        "cli.repository.checkout",
        "cli.file.read",
        "cli.test.run",
        "cli.file.edit",
        "cli.test.run",
        "cli.repository.push",
        "github.pull_request.create",
        "system.trigger.sync_external",
    ], calls
    assert all(c["status"] == "completed" for c in calls), calls
    assert calls[2]["sanitized_output_json"]["passed"] is False  # red before the fix
    assert calls[4]["sanitized_output_json"]["passed"] is True  # green after
    pr_number = calls[6]["sanitized_output_json"]["number"]

    timeline = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run_id}/timeline")
    sandbox_events = [e for e in timeline if e["event_type"] == "sandbox.job"]
    assert len(sandbox_events) == 6, [e["event_type"] for e in timeline]

    # The PR is real in fake GitHub, from our agent branch into main.
    async with httpx.AsyncClient(timeout=10.0) as anon:
        gh_state = (await anon.get(f"{FAKE_GITHUB_HOST}/_state")).json()
    repo = gh_state["repos"]["octo/alpha"]
    assert branch in repo["branches"], sorted(repo["branches"])
    pull = repo["pulls"][str(pr_number)]
    assert pull["head"]["ref"] == branch
    assert pull["base"]["ref"] == "main"

    # Comment-back: the outcome comment (with our trigger's name) is visible
    # on the issue in fake Linear. Sync runs after task completion; poll.
    deadline = time.monotonic() + 30.0
    ours: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        comments = (await _fake_state())["comments"]["ENG-142"]
        ours = [c for c in comments if f"P7 slice {tag}" in c["body"]]
        if ours:
            break
        await asyncio.sleep(1.0)
    assert len(ours) == 1, f"expected exactly one comment-back, got {ours}"
    assert "completed" in ours[0]["body"]


# --- (b) DUPLICATE DELIVERY ---


async def test_duplicate_deliveries_never_duplicate_work(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag)  # no tools; the run just replies
    linear, _secret = await _linear_connection(client, ws, tag)
    trigger = await _make_trigger(
        client,
        ws,
        name=f"P7 dupes {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
        dedupe_window_seconds=3600,
    )

    identifier = await _new_issue(f"Dupe probe {tag}", "Reply with a plan, no tools needed.")
    await _settle_trigger_cache()
    delivery = await _transition(identifier, "Todo")
    rows = await _wait_invocations(client, ws, trigger["id"], minimum=1)
    assert [r["status"] for r in rows] == ["started"], rows

    # Layer 1 — webhook delivery dedupe: byte-for-byte redelivery with the
    # SAME delivery id. The webhook table absorbs it; nothing new reaches
    # the trigger engine.
    redelivered = await _fake_admin("/_admin/redeliver", {"delivery_id": delivery["delivery_id"]})
    assert redelivered["response"] == 202  # acked so the provider stops retrying
    await asyncio.sleep(QUIET_SECONDS)
    rows = await _invocations(client, ws, trigger["id"])
    assert [r["status"] for r in rows] == ["started"], rows

    # Layer 2 — trigger idempotency key: a semantically identical event
    # (same transition content, brand-NEW delivery id, fresh timestamp)
    # within the dedupe window records a `duplicate` invocation and starts
    # nothing.
    refired = await _fake_admin("/_admin/refire", {"delivery_id": delivery["delivery_id"]})
    assert refired["delivery_id"] != delivery["delivery_id"]
    assert refired["response"] == 202
    rows = await _wait_invocations(client, ws, trigger["id"], minimum=2)
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["duplicate", "started"], rows
    started = [r for r in rows if r["status"] == "started"]
    duplicate = [r for r in rows if r["status"] == "duplicate"]
    # Same deterministic Temporal workflow id on both rows: even if the DB
    # race were lost, Temporal's duplicate-start policy is the second
    # defense (unit-tested via WorkflowAlreadyStartedError in the matcher).
    assert duplicate[0]["workflow_id"] == started[0]["workflow_id"]

    # Still exactly one task for this agent and this issue.
    tasks = await _tasks_for_agent(client, ws, agent["id"])
    assert len(tasks) == 1, tasks
    assert tasks[0]["external_id"] == identifier


# --- (c) NO-TRANSITION ---


async def test_update_without_state_change_does_not_invoke(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag)
    linear, _secret = await _linear_connection(client, ws, tag)
    trigger = await _make_trigger(
        client,
        ws,
        name=f"P7 no-transition {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
    )

    identifier = await _new_issue(f"Edit probe {tag}", "Reply briefly.")
    await _settle_trigger_cache()
    await _transition(identifier, "Todo")
    rows = await _wait_invocations(client, ws, trigger["id"], minimum=1)
    assert len(rows) == 1

    # A title edit while the issue REMAINS in Todo: updatedFrom carries the
    # old title but no stateId, so `transitioned_to` must not fire.
    edited = await _fake_admin(
        f"/_admin/issues/{identifier}/edit", {"title": f"Edited title {tag}"}
    )
    assert edited["response"] == 202
    await asyncio.sleep(QUIET_SECONDS)
    rows = await _invocations(client, ws, trigger["id"])
    assert len(rows) == 1, f"title edit must not invoke the trigger: {rows}"


# --- (d) FILTER MISS ---


async def test_transition_on_other_team_misses_filter(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag)
    linear, _secret = await _linear_connection(client, ws, tag)
    # The trigger under test watches a team that will never match (OPS);
    # a control trigger on ENG proves the event flowed end-to-end.
    miss = await _make_trigger(
        client,
        ws,
        name=f"P7 miss {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
        team_key="OPS",
    )
    control = await _make_trigger(
        client,
        ws,
        name=f"P7 control {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
        team_key="ENG",
    )

    identifier = await _new_issue(f"Team probe {tag}", "Reply briefly.")
    await _settle_trigger_cache()
    await _transition(identifier, "Todo")

    await _wait_invocations(client, ws, control["id"], minimum=1)  # event processed
    rows = await _invocations(client, ws, miss["id"])
    assert rows == [], f"OPS-filtered trigger must not fire on an ENG transition: {rows}"


# --- (e) TRIGGER TEST ENDPOINT ---


async def test_trigger_test_endpoint_explains_conditions(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag)
    linear, _secret = await _linear_connection(client, ws, tag)
    trigger = await _make_trigger(
        client,
        ws,
        name=f"P7 dry-run {tag}",
        connection_id=linear["id"],
        agent_id=agent["id"],
    )

    def event(team: str, state: str, *, transitioned: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "external_id": "ENG-142",
            "team": {"key": team},
            "state": {"name": state},
        }
        if transitioned:
            data["changed_from"] = {"state": {"id": "prev-state"}}
        return {"event_type": "connector.linear.issue.updated", "data": data}

    # A real Backlog→Todo transition on ENG: both conditions pass.
    result = await _post(
        client,
        f"/api/v1/workspaces/{ws}/triggers/{trigger['id']}/test",
        {"event": event("ENG", "Todo", transitioned=True)},
        expect=200,
    )
    assert result["matched"] is True
    assert result["event_type_matches"] is True
    assert [c["passed"] for c in result["conditions"]] == [True, True], result

    # Same shape but the state did not change: the transitioned_to condition
    # fails with an explanation, the team condition still passes.
    result = await _post(
        client,
        f"/api/v1/workspaces/{ws}/triggers/{trigger['id']}/test",
        {"event": event("ENG", "Todo", transitioned=False)},
        expect=200,
    )
    assert result["matched"] is False
    by_path = {c["path"]: c for c in result["conditions"]}
    assert by_path["data.team.key"]["passed"] is True
    todo = by_path["data.state.name"]
    assert todo["passed"] is False
    assert todo["op"] == "transitioned_to"
    assert todo["detail"], "failed conditions must explain themselves"

    # Wrong team: the eq condition fails and reports the actual value.
    result = await _post(
        client,
        f"/api/v1/workspaces/{ws}/triggers/{trigger['id']}/test",
        {"event": event("OPS", "Todo", transitioned=True)},
        expect=200,
    )
    assert result["matched"] is False
    team = {c["path"]: c for c in result["conditions"]}["data.team.key"]
    assert team["passed"] is False
    assert team["actual"] == "OPS"
