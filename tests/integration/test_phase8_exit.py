"""Phase 8 exit tests (plan 45): hierarchical work — delegation, the QA
failure→fix→retest loop, and concurrency controls.

(a) BLOCKING DELEGATION — an SWE implements a fix, opens a PR, then delegates
    a QA review (blocking review_request). The QA agent checks out the PR
    branch in a sandbox, runs the tests, and reports an evidence-based pass.
    The parent run parks (waiting_delegation), resumes on the standardized
    summary — never the child transcript — and completes. Child task/run
    rows, structured messages, lineage tree, and audit are all verified.
(b) FAILURE LOOP — through the engineering template (direct mode): the SWE
    pushes a WRONG fix; QA fails the review with real test evidence; the SWE
    gets a fix child task carrying the failure context; QA retests and
    passes. Bounded loop, full parent/child lineage, and manager-summary
    observations asserted at transcript level.
(c) MANAGER ROUTING — coordinator mode: the trigger targets the CTO,
    implementation is delegated to the SWE and review to QA through the
    engineering template path. The CTO never runs a model.
(d) DELEGATION PERMISSION — deny-by-default: no organization.delegate grant
    → denied; a target outside the granted relationship (non-subordinate) →
    denied; the workspace depth limit → denied. All decided by the policy
    engine (error codes on tool_call rows), never by model text.
(e) CONCURRENCY — an agent with max_concurrent_runs=1 gets two tasks: the
    second queues visibly (reason recorded) and starts only after the first
    completes; both the parked first run and the queued second survive an
    agent-worker restart mid-queue.

Scripting notes: the fake provider consumes [[tool:...]] markers from system
and user messages. Markers that must fire N conversation hops away are
base64-wrapped N times ([[b64:...]] unwraps exactly one layer per scan), and
__VERDICT__ resolves from the latest tool result's exit_code — so QA verdicts
are grounded in actual test runs, not scripted assertions.
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
from jhin_models.testing.fake_openai import encode_marker_payload

from .conftest import API_URL, FAKE_LINEAR_URL, compose

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
FAKE_GITHUB_INTERNAL = "http://fake-github:8080"
FAKE_GITHUB_PAT = "fake-github-pat"
FAKE_LINEAR_INTERNAL = "http://fake-linear:8080"
FAKE_LINEAR_HOST = FAKE_LINEAR_URL
FAKE_LINEAR_API_KEY = "fake-linear-api-key"
API_INTERNAL_URL = "http://api:8000"

TASK_TIMEOUT_SECONDS = 420.0
INVOCATION_TIMEOUT_SECONDS = 60.0


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


async def _patch(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.patch(path, json=body, headers=_csrf(client))
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    payload: dict[str, Any] = response.json()
    return payload


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params or None)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    return response.json()


# --- wiring helpers ---


async def _connections(
    client: httpx.AsyncClient, ws: str, tag: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One GitHub + one CLI connection (sandbox git ops via the GitHub PAT)."""
    github = (
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/connections",
            {
                "connector_type": "github",
                "name": f"P8 GitHub {tag}",
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
                "name": f"P8 CLI {tag}",
                "auth_type": "none",
                "credentials": {},
                "config": {"default_network": "none", "git_connection_id": github["id"]},
            },
        )
    )["connection"]
    return github, cli


async def _make_agent(
    client: httpx.AsyncClient,
    ws: str,
    name: str,
    *,
    system_prompt: str = "You are an agent; use tools when instructed.",
    manager_agent_id: str | None = None,
    grants: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P8 provider {name}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P8 profile {name}",
        },
    )
    agent = await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": name,
            "system_prompt": system_prompt,
            "model_profile_id": profile["id"],
            "manager_agent_id": manager_agent_id,
        },
    )
    for capability, scope in (grants or {}).items():
        await _post(
            client,
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/grants",
            {"capability": capability, "scope": scope, "effect": "allow"},
        )
    return agent


def _swe_grants(cli_id: str, github_id: str) -> dict[str, dict[str, str]]:
    return {
        "cli.repository.checkout": {"connection_id": cli_id, "repository": "octo/alpha"},
        "cli.file.read": {"connection_id": cli_id, "path": "*"},
        "cli.file.write": {"connection_id": cli_id, "path": "*"},
        "cli.test.run": {"connection_id": cli_id, "command": "bash *"},
        "cli.command.execute": {"connection_id": cli_id, "command": "git *"},
        "github.pull_request.create": {"connection_id": github_id, "repository": "octo/alpha"},
    }


def _qa_grants(cli_id: str) -> dict[str, dict[str, str]]:
    """Plan 27 defaults: QA gets read/test sandbox access + result reporting."""
    return {
        "cli.repository.checkout": {"connection_id": cli_id, "repository": "octo/alpha"},
        "cli.test.run": {"connection_id": cli_id, "command": "bash *"},
        "organization.report_result": {},
    }


async def _assign(
    client: httpx.AsyncClient, ws: str, agent_id: str, title: str, description: str
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/assign-task",
        {"title": title, "description": description},
    )


async def _task(client: httpx.AsyncClient, ws: str, task_id: str) -> dict[str, Any]:
    detail: dict[str, Any] = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}")
    return detail


async def _wait_task_finished(
    client: httpx.AsyncClient,
    ws: str,
    task_id: str,
    *,
    budget: float = TASK_TIMEOUT_SECONDS,
    observe: set[str] | None = None,
) -> dict[str, Any]:
    """Poll until terminal; optionally collect run statuses seen on the way."""
    deadline = time.monotonic() + budget
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _task(client, ws, task_id)
        if observe is not None:
            for run in detail["runs"]:
                observe.add(run["status"])
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(1.0)
    pytest.fail(f"task {task_id} did not finish in {budget}s: {detail['task']}")


async def _wait_engineering_result(
    client: httpx.AsyncClient, ws: str, task_id: str, *, budget: float = TASK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Wait until the template's finalize activity recorded the outcome."""
    deadline = time.monotonic() + budget
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _task(client, ws, task_id)
        if detail["task"]["metadata_json"].get("engineering_result"):
            return detail
        await asyncio.sleep(1.0)
    pytest.fail(f"task {task_id}: no engineering_result in {budget}s: {detail['task']}")


async def _messages(client: httpx.AsyncClient, ws: str, task_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/tasks/{task_id}/messages"
    )
    return result


async def _tool_calls(client: httpx.AsyncClient, ws: str, run_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls"
    )
    return result


async def _tree_children(
    client: httpx.AsyncClient, ws: str, task_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tree = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}/tree")
    root = tree["root"]
    return root, sorted(root["children"], key=lambda n: n["task"]["created_at"])


async def _tasks_for_agent(
    client: httpx.AsyncClient, ws: str, agent_id: str
) -> list[dict[str, Any]]:
    listing = await _get(client, f"/api/v1/workspaces/{ws}/tasks", agent_id=agent_id, limit=50)
    items: list[dict[str, Any]] = listing["items"]
    return items


async def _audit_actions(
    client: httpx.AsyncClient, ws: str, action: str, target_id: str
) -> list[dict[str, Any]]:
    page = await _get(client, f"/api/v1/workspaces/{ws}/audit-events", action=action, limit=100)
    return [e for e in page["events"] if str(e.get("target_id")) == target_id]


# --- fake Linear driving (for the template tests) ---


async def _fake_admin(path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as anon:
        response = await anon.post(f"{FAKE_LINEAR_HOST}{path}", json=body)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
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
    state = await _fake_state()
    team_id = state["teams"]["ENG"]["id"]
    data = await _fake_graphql(
        "mutation issueCreate($input: IssueCreateInput!) { issueCreate(input: $input) "
        "{ success issue { identifier } } }",
        {"input": {"teamId": team_id, "title": title, "description": description}},
    )
    identifier: str = data["issueCreate"]["issue"]["identifier"]
    return identifier


async def _linear_connection(client: httpx.AsyncClient, ws: str, tag: str) -> dict[str, Any]:
    created = await _post(
        client,
        f"/api/v1/workspaces/{ws}/connections",
        {
            "connector_type": "linear",
            "name": f"P8 Linear {tag}",
            "auth_type": "api_key",
            "credentials": {"api_key": FAKE_LINEAR_API_KEY},
            "config": {"base_url": FAKE_LINEAR_INTERNAL},
        },
    )
    webhook = created["webhook"]
    await _fake_admin(
        "/_admin/webhook",
        {"url": f"{API_INTERNAL_URL}{webhook['url_path']}", "secret": webhook["secret"]},
    )
    connection: dict[str, Any] = created["connection"]
    return connection


async def _engineering_trigger(
    client: httpx.AsyncClient,
    ws: str,
    *,
    name: str,
    connection_id: str,
    target_agent_id: str,
    workflow_definition: dict[str, Any],
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
                    {"path": "data.team.key", "op": "eq", "value": "ENG"},
                    {"path": "data.state.name", "op": "transitioned_to", "value": "Todo"},
                ]
            },
            "target_agent_id": target_agent_id,
            "action_config": {"comment_back": False},
            "dedupe_window_seconds": 3600,
            "workflow_definition": workflow_definition,
        },
    )


async def _template_task_id(
    client: httpx.AsyncClient, ws: str, trigger_id: str, identifier: str
) -> str:
    """Fire the Backlog→Todo transition and return the created task id."""
    await asyncio.sleep(6.0)  # matcher trigger-cache TTL
    result = await _fake_admin(f"/_admin/issues/{identifier}/transition", {"state": "Todo"})
    assert result.get("delivered") is True and result.get("response") == 202, result
    deadline = time.monotonic() + INVOCATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        rows = await _get(client, f"/api/v1/workspaces/{ws}/triggers/{trigger_id}/invocations")
        if rows and rows[0].get("task_id"):
            task_id: str = rows[0]["task_id"]
            return task_id
        await asyncio.sleep(1.0)
    pytest.fail(f"trigger {trigger_id} never linked a task")


# --- marker script builders ---


def _implement_markers(cli_id: str, github_id: str, branch: str, tag: str, *, value: int) -> str:
    """checkout → write app.py (VALUE=<value>) → test → push → PR.
    value=2 is the correct fix (tests green); anything else leaves them red."""
    push = f"git add -A && git commit -m fix-value && git push origin {branch}"
    return " ".join(
        [
            f'[[tool:cli.repository.checkout {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "branch": "{branch}"}}]]',
            f'[[tool:cli.file.write {{"connection_id": "{cli_id}", "path": "app.py", '
            f'"content": "VALUE = {value}\\n"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:cli.command.execute {{"connection_id": "{cli_id}", '
            f'"command": "{push}", "network": "internet"}}]]',
            f'[[tool:github.pull_request.create {{"connection_id": "{github_id}", '
            f'"repository": "octo/alpha", "title": "Fix VALUE {tag}", '
            f'"head": "{branch}", "base": "main", "body": "Automated by Jhin."}}]]',
        ]
    )


def _qa_review_markers(cli_id: str, branch: str, work_branch: str, summary: str) -> str:
    """checkout the PR branch → run tests → report an evidence-based verdict."""
    return " ".join(
        [
            f'[[tool:cli.repository.checkout {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "ref": "{branch}", "branch": "{work_branch}"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:organization.report_result {{"status": "__VERDICT__", '
            f'"summary": "{summary}", '
            f'"recommended_next_action": "merge if passing, fix and retest if failing"}}]]',
        ]
    )


def _fix_markers(cli_id: str, branch: str, tag: str) -> str:
    """Clone the PR branch, apply the correct fix, prove green, push back."""
    push = f"git add -A && git commit -m qa-fix && git push origin HEAD:{branch}"
    return " ".join(
        [
            f'[[tool:cli.repository.checkout {{"connection_id": "{cli_id}", '
            f'"repository": "octo/alpha", "ref": "{branch}", "branch": "fix/{tag}"}}]]',
            f'[[tool:cli.file.write {{"connection_id": "{cli_id}", "path": "app.py", '
            f'"content": "VALUE = 2\\n"}}]]',
            f'[[tool:cli.test.run {{"connection_id": "{cli_id}", '
            f'"command": "bash ./run_tests.sh"}}]]',
            f'[[tool:cli.command.execute {{"connection_id": "{cli_id}", '
            f'"command": "{push}", "network": "internet"}}]]',
        ]
    )


def _by_type(messages: list[dict[str, Any]], message_type: str) -> list[dict[str, Any]]:
    return [m for m in messages if m["message_type"] == message_type]


# --- (a) BLOCKING DELEGATION: SWE → QA review, pass, parent resumes ---


async def test_blocking_qa_delegation_pass_resumes_parent(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    branch = f"agent/p8a-{tag}"
    github, cli = await _connections(client, ws, tag)

    qa = await _make_agent(client, ws, f"P8 QA {tag}", grants=_qa_grants(cli["id"]))
    swe = await _make_agent(
        client,
        ws,
        f"P8 SWE {tag}",
        grants={
            **_swe_grants(cli["id"], github["id"]),
            # Delegation permission: the policy model is exercised properly in
            # test (d); here the SWE may delegate to any active agent.
            "organization.delegate": {"targets": "any"},
        },
    )

    # The QA script rides inside the delegation instructions, double-encoded:
    # the SWE's scan unwraps one layer (leaving an inert blob in the marker
    # JSON); the QA child's scan unwraps the second into real markers.
    qa_script = _qa_review_markers(
        cli["id"], branch, f"qa/p8a-{tag}", f"Checked out {branch} and ran the suite; P8A {tag}"
    )
    qa_blob = encode_marker_payload(encode_marker_payload(qa_script))
    description = " ".join(
        [
            _implement_markers(cli["id"], github["id"], branch, tag, value=2),
            f'[[tool:organization.delegate_task {{"target_agent_id": "{qa["id"]}", '
            f'"title": "QA review {tag}", '
            f'"instructions": "Review the PR branch {branch}. {qa_blob}", '
            f'"expected_output": "pass/fail verdict with test evidence", '
            f'"blocking": true, "kind": "review_request", '
            f'"artifacts": [{{"type": "branch", "id": "{branch}"}}]}}]]',
        ]
    )

    parent = await _assign(client, ws, swe["id"], f"P8a implement+review {tag}", description)
    observed: set[str] = set()
    detail = await _wait_task_finished(client, ws, parent["id"], observe=observed)
    assert detail["task"]["state"] == "completed", detail["task"]
    assert "waiting_delegation" in observed, (
        f"parent run must park durably while the child works; saw {observed}"
    )
    assert len(detail["runs"]) == 1
    parent_run = detail["runs"][0]
    assert parent_run["status"] == "completed"

    # Lineage: exactly one child task, assigned to QA, completed.
    root, children = await _tree_children(client, ws, parent["id"])
    assert root["task"]["id"] == parent["id"]
    assert len(children) == 1, children
    child = children[0]
    assert child["task"]["parent_task_id"] == parent["id"]
    assert child["agent_name"] == qa["name"]
    assert child["task"]["state"] == "completed"
    assert child["latest_run_status"] == "completed"
    child_meta = child["task"]["metadata_json"]["delegation"]
    assert child_meta["kind"] == "review_request"
    assert child_meta["blocking"] is True
    assert child_meta["delegated_by_agent_id"] == swe["id"]

    # Structured messages on the parent: the review_request out, the
    # review_result back — a summary, never the child transcript (plan 7.6).
    messages = await _messages(client, ws, parent["id"])
    requests = _by_type(messages, "review_request")
    assert len(requests) == 1
    assert requests[0]["content_json"]["child_task_id"] == child["task"]["id"]
    assert requests[0]["content_json"]["target_agent_name"] == qa["name"]
    results = _by_type(messages, "review_result")
    assert len(results) == 1
    verdict_content = results[0]["content_json"]
    assert verdict_content["verdict"] == "pass"
    assert verdict_content["reported"] is True
    assert verdict_content["from_agent_name"] == qa["name"]
    assert f"P8A {tag}" in verdict_content["summary"]
    assert "stdout" not in str(verdict_content), "summaries must not carry raw tool output"

    # Parent transcript: the scripted SWE flow, then the delegation tool call
    # whose (deferred) result is the standardized summary.
    calls = await _tool_calls(client, ws, parent_run["id"])
    assert [c["tool_name"] for c in calls] == [
        "cli.repository.checkout",
        "cli.file.write",
        "cli.test.run",
        "cli.command.execute",
        "github.pull_request.create",
        "organization.delegate_task",
    ], calls
    assert all(c["status"] == "completed" for c in calls), calls
    assert calls[5]["sanitized_output_json"]["child_task_id"] == child["task"]["id"]

    # Child transcript: checkout the PR branch, run the suite (green),
    # report the structured result.
    child_detail = await _task(client, ws, child["task"]["id"])
    child_calls = await _tool_calls(client, ws, child_detail["runs"][0]["id"])
    assert [c["tool_name"] for c in child_calls] == [
        "cli.repository.checkout",
        "cli.test.run",
        "organization.report_result",
    ], child_calls
    assert child_calls[1]["sanitized_output_json"]["passed"] is True
    assert child_calls[2]["sanitized_output_json"]["status"] == "pass"

    # Append-only audit: the delegation and the review report.
    assert await _audit_actions(client, ws, "task.delegated", child["task"]["id"])
    assert await _audit_actions(client, ws, "task.review_reported", child["task"]["id"])


# --- (b) FAILURE LOOP: wrong fix → QA fail → fix task → retest → pass ---


async def test_engineering_template_failure_fix_retest_loop(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    branch = f"agent/p8b-{tag}"
    github, cli = await _connections(client, ws, tag)

    # The QA reviewer is scripted on its system prompt (template-composed
    # review tasks carry no markers). Its failure summary smuggles the FIX
    # script for the implementer, double-encoded — after QA's own scan
    # unwraps one layer, the reported summary carries an inert blob that the
    # fix child task (whose instructions embed the failure context) decodes.
    fix_blob = encode_marker_payload(encode_marker_payload(_fix_markers(cli["id"], branch, tag)))
    qa_prompt = "You are QA. For any review task, do exactly this: " + _qa_review_markers(
        cli["id"], branch, f"qa/p8b-{tag}", f"Suite verdict for {branch} from exit code. {fix_blob}"
    )
    # The retest instructions echo the cycle-1 failure summary, so in cycle 2
    # the scripted provider also replays the smuggled fix script after QA's
    # own verdict. Since the Phase 10 tool-worker boundary a denied call stops
    # the run, hence QA additionally holds the implementer grants: the replayed
    # script then re-applies an already-pushed fix (a no-op) instead of
    # failing the passing review with `no_grant`.
    qa = await _make_agent(
        client,
        ws,
        f"P8b QA {tag}",
        system_prompt=qa_prompt,
        grants={**_swe_grants(cli["id"], github["id"]), **_qa_grants(cli["id"])},
    )
    swe = await _make_agent(
        client, ws, f"P8b SWE {tag}", grants=_swe_grants(cli["id"], github["id"])
    )

    linear = await _linear_connection(client, ws, tag)
    trigger = await _engineering_trigger(
        client,
        ws,
        name=f"P8b loop {tag}",
        connection_id=linear["id"],
        target_agent_id=swe["id"],
        workflow_definition={
            "template": "engineering_ticket",
            "qa_agent_id": qa["id"],
            "max_retest_cycles": 3,
        },
    )

    # The issue scripts a WRONG fix (VALUE = 3): the suite stays red, but the
    # SWE pushes and opens the PR anyway — exactly the case QA exists for.
    identifier = await _new_issue(
        f"P8b wrong fix {tag}",
        _implement_markers(cli["id"], github["id"], branch, tag, value=3),
    )
    task_id = await _template_task_id(client, ws, trigger["id"], identifier)
    detail = await _wait_engineering_result(client, ws, task_id, budget=480.0)

    outcome = detail["task"]["metadata_json"]["engineering_result"]
    assert outcome == {"status": "completed", "verdict": "pass", "cycles_used": 2}, outcome
    assert detail["task"]["state"] == "completed"

    # Lineage: QA review (cycle 1, failed) → fix (cycle 1) → QA review
    # (cycle 2, passed) — the loop terminated well under the bound of 3.
    _root, children = await _tree_children(client, ws, task_id)
    kinds = [c["task"]["metadata_json"]["delegation"]["kind"] for c in children]
    assert kinds == ["review_request", "delegation", "review_request"], kinds
    review1, fix, review2 = children
    assert review1["agent_name"] == qa["name"]
    assert fix["agent_name"] == swe["name"]
    assert review2["agent_name"] == qa["name"]
    assert fix["task"]["title"].startswith("Fix (cycle 1):")
    # The fix task carries the failure context (plan 27), not a transcript.
    assert "The review failed" in fix["task"]["description"]
    assert f"Suite verdict for {branch}" in fix["task"]["description"]
    assert all(c["task"]["state"] == "completed" for c in children)

    # Transcript-level proof of the loop:
    # cycle 1 review — the suite genuinely failed and QA reported "fail".
    r1 = await _task(client, ws, review1["task"]["id"])
    r1_calls = await _tool_calls(client, ws, r1["runs"][0]["id"])
    assert [c["tool_name"] for c in r1_calls] == [
        "cli.repository.checkout",
        "cli.test.run",
        "organization.report_result",
    ]
    assert r1_calls[1]["sanitized_output_json"]["passed"] is False
    assert r1_calls[2]["sanitized_output_json"]["status"] == "fail"
    # the fix — correct change, suite green, pushed back to the PR branch.
    fx = await _task(client, ws, fix["task"]["id"])
    fx_calls = await _tool_calls(client, ws, fx["runs"][0]["id"])
    assert [c["tool_name"] for c in fx_calls] == [
        "cli.repository.checkout",
        "cli.file.write",
        "cli.test.run",
        "cli.command.execute",
    ]
    assert fx_calls[2]["sanitized_output_json"]["passed"] is True
    # cycle 2 review — retest on the updated branch, evidence-based pass.
    r2 = await _task(client, ws, review2["task"]["id"])
    r2_calls = await _tool_calls(client, ws, r2["runs"][0]["id"])
    assert r2_calls[1]["sanitized_output_json"]["passed"] is True
    assert r2_calls[2]["sanitized_output_json"]["status"] == "pass"

    # Structured messages on the main task: fail then pass review_results
    # (manager summaries — no raw tool output), plus the final status line.
    messages = await _messages(client, ws, task_id)
    verdicts = [m["content_json"]["verdict"] for m in _by_type(messages, "review_result")]
    assert verdicts == ["fail", "pass"], verdicts
    for m in _by_type(messages, "review_result"):
        assert "stdout" not in str(m["content_json"])
    finals = [
        m
        for m in _by_type(messages, "status")
        if m["content_json"].get("template") == "engineering_ticket"
    ]
    assert len(finals) == 1
    assert finals[0]["content_json"]["cycles_used"] == 2
    assert await _audit_actions(client, ws, "task.engineering_finished", task_id)


# --- (c) MANAGER ROUTING: CTO coordinates through the template ---


async def test_engineering_template_coordinator_mode_routes_via_cto(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    branch = f"agent/p8c-{tag}"
    github, cli = await _connections(client, ws, tag)

    cto = await _make_agent(client, ws, f"P8c CTO {tag}")
    qa_prompt = "You are QA. For any review task, do exactly this: " + _qa_review_markers(
        cli["id"], branch, f"qa/p8c-{tag}", f"Reviewed {branch}; verdict from the suite."
    )
    qa = await _make_agent(
        client, ws, f"P8c QA {tag}", system_prompt=qa_prompt, grants=_qa_grants(cli["id"])
    )
    swe = await _make_agent(
        client,
        ws,
        f"P8c SWE {tag}",
        manager_agent_id=cto["id"],
        grants={**_swe_grants(cli["id"], github["id"]), "organization.report_result": {}},
    )

    linear = await _linear_connection(client, ws, tag)
    trigger = await _engineering_trigger(
        client,
        ws,
        name=f"P8c coordinator {tag}",
        connection_id=linear["id"],
        target_agent_id=cto["id"],
        workflow_definition={
            "template": "engineering_ticket",
            "implementer_agent_id": swe["id"],
            "qa_agent_id": qa["id"],
            "max_retest_cycles": 3,
        },
    )

    # Correct fix this time; the SWE also reports a structured result whose
    # artifact flows into the QA review request.
    description = " ".join(
        [
            _implement_markers(cli["id"], github["id"], branch, tag, value=2),
            f'[[tool:organization.report_result {{"status": "completed", '
            f'"summary": "Implemented {tag} and opened the PR.", '
            f'"artifacts": [{{"type": "branch", "id": "{branch}"}}]}}]]',
        ]
    )
    identifier = await _new_issue(f"P8c coordinator {tag}", description)
    task_id = await _template_task_id(client, ws, trigger["id"], identifier)
    detail = await _wait_engineering_result(client, ws, task_id)

    outcome = detail["task"]["metadata_json"]["engineering_result"]
    assert outcome == {"status": "completed", "verdict": "pass", "cycles_used": 1}, outcome
    task = detail["task"]
    assert task["state"] == "completed"
    assert task["assigned_agent_id"] == cto["id"]
    # Coordinator mode: the CTO owns the ticket but never runs a model.
    assert detail["runs"] == [], detail["runs"]

    # Both hops are children of the CTO's task, delegated by the CTO.
    _root, children = await _tree_children(client, ws, task_id)
    assert [c["agent_name"] for c in children] == [swe["name"], qa["name"]], children
    impl, review = children
    assert impl["task"]["metadata_json"]["delegation"]["kind"] == "delegation"
    assert impl["task"]["metadata_json"]["delegation"]["delegated_by_agent_id"] == cto["id"]
    assert impl["task"]["title"].startswith("Implement:")
    assert review["task"]["metadata_json"]["delegation"]["kind"] == "review_request"
    assert all(c["task"]["state"] == "completed" for c in children)

    # The implementer's reported artifact rode into the review request.
    messages = await _messages(client, ws, task_id)
    requests = _by_type(messages, "review_request")
    assert len(requests) == 1
    artifact_ids = [a.get("id") for a in requests[0]["content_json"]["artifacts"]]
    assert branch in artifact_ids, requests[0]["content_json"]
    delegations = _by_type(messages, "delegation")
    assert len(delegations) == 1
    assert delegations[0]["content_json"]["target_agent_name"] == swe["name"]
    results = _by_type(messages, "result")
    assert any(f"Implemented {tag}" in m["content_json"]["summary"] for m in results)
    verdicts = [m["content_json"]["verdict"] for m in _by_type(messages, "review_result")]
    assert verdicts == ["pass"], verdicts

    # The SWE child genuinely did the work (PR opened from its transcript).
    impl_detail = await _task(client, ws, impl["task"]["id"])
    impl_calls = await _tool_calls(client, ws, impl_detail["runs"][0]["id"])
    assert "github.pull_request.create" in [c["tool_name"] for c in impl_calls]


# --- (d) DELEGATION PERMISSION: deny-by-default, relationship, depth ---


async def test_delegation_denied_without_grant_and_outside_policy(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    target = await _make_agent(client, ws, f"P8d target {tag}")

    def delegate_marker(target_id: str) -> str:
        return (
            f'[[tool:organization.delegate_task {{"target_agent_id": "{target_id}", '
            f'"title": "probe {tag}", "instructions": "do something", "blocking": false}}]]'
        )

    # 1) No organization.delegate grant at all → deny (no_grant).
    no_grant = await _make_agent(client, ws, f"P8d nogrant {tag}")
    task = await _assign(
        client, ws, no_grant["id"], f"P8d no-grant {tag}", delegate_marker(target["id"])
    )
    detail = await _wait_task_finished(client, ws, task["id"], budget=120.0)
    calls = await _tool_calls(client, ws, detail["runs"][0]["id"])
    assert calls[0]["tool_name"] == "organization.delegate_task"
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "no_grant", calls[0]

    # 2) Grant present but default scope (subordinates) and the target is not
    #    a subordinate → deny (relationship mismatch).
    scoped = await _make_agent(
        client, ws, f"P8d scoped {tag}", grants={"organization.delegate": {}}
    )
    task = await _assign(
        client, ws, scoped["id"], f"P8d non-subordinate {tag}", delegate_marker(target["id"])
    )
    detail = await _wait_task_finished(client, ws, task["id"], budget=120.0)
    calls = await _tool_calls(client, ws, detail["runs"][0]["id"])
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "delegation_target_not_permitted", calls[0]

    # No child task ever reached the target agent.
    assert await _tasks_for_agent(client, ws, target["id"]) == []

    # 3) Depth limit: with max_task_depth=1 the root may delegate once, but
    #    the delegated child may not go deeper. The chain's second hop is
    #    smuggled into the first delegation's instructions (double-encoded).
    await _patch(
        client, f"/api/v1/workspaces/{ws}", {"settings": {"delegation": {"max_task_depth": 1}}}
    )
    try:
        deep = await _make_agent(client, ws, f"P8d deep {tag}")
        mid = await _make_agent(
            client, ws, f"P8d mid {tag}", grants={"organization.delegate": {"targets": "any"}}
        )
        top = await _make_agent(
            client, ws, f"P8d top {tag}", grants={"organization.delegate": {"targets": "any"}}
        )
        onward = encode_marker_payload(encode_marker_payload(delegate_marker(deep["id"])))
        task = await _assign(
            client,
            ws,
            top["id"],
            f"P8d depth {tag}",
            f'[[tool:organization.delegate_task {{"target_agent_id": "{mid["id"]}", '
            f'"title": "hop 1 {tag}", "instructions": "Go deeper. {onward}", '
            f'"blocking": true}}]]',
        )
        await _wait_task_finished(client, ws, task["id"], budget=180.0)

        mid_tasks = await _tasks_for_agent(client, ws, mid["id"])
        assert len(mid_tasks) == 1, "the first hop is within the depth limit"
        mid_detail = await _task(client, ws, mid_tasks[0]["id"])
        mid_calls = await _tool_calls(client, ws, mid_detail["runs"][0]["id"])
        assert mid_calls[0]["tool_name"] == "organization.delegate_task"
        assert mid_calls[0]["status"] == "denied"
        assert mid_calls[0]["error_code"] == "delegation_depth_exceeded", mid_calls[0]
        assert await _tasks_for_agent(client, ws, deep["id"]) == []
    finally:
        await _patch(
            client, f"/api/v1/workspaces/{ws}", {"settings": {"delegation": {"max_task_depth": 5}}}
        )


# --- (e) CONCURRENCY: queue, don't reject — durable across a restart ---


async def test_concurrency_queues_second_task_and_survives_worker_restart(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]

    # max_concurrent_runs defaults to 1. Task 1 parks on an approval gate
    # (waiting_approval holds the slot); task 2 must queue behind it.
    agent = await _make_agent(
        client,
        ws,
        f"P8e worker {tag}",
        grants={"system.demo.destructive": {}},
    )

    first = await _assign(
        client,
        ws,
        agent["id"],
        f"P8e first {tag}",
        f'[[tool:system.demo.destructive {{"label": "hold-slot-{tag}"}}]]',
    )

    # Wait until task 1 genuinely holds the slot (run parked on approval).
    approval_id = ""
    first_detail: dict[str, Any] = {}
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        pending = await _get(client, f"/api/v1/workspaces/{ws}/approvals", status="pending")
        first_detail = await _task(client, ws, first["id"])
        parked_run = next(
            (run for run in first_detail["runs"] if run["status"] == "waiting_approval"),
            None,
        )
        for row in pending["items"]:
            if row.get("task_id") == first["id"]:
                approval_id = row["id"]
        if parked_run is not None and approval_id:
            break
        await asyncio.sleep(1.0)
    assert approval_id, "task 1 never parked on its approval"
    parked_run = next(
        (run for run in first_detail["runs"] if run["status"] == "waiting_approval"),
        None,
    )
    assert parked_run is not None, first_detail

    second = await _assign(
        client, ws, agent["id"], f"P8e second {tag}", "Reply with a one-line status. No tools."
    )

    # Task 2 queues visibly: state + recorded reason + audit, and no run row.
    deadline = time.monotonic() + 60.0
    queued: dict[str, Any] = {}
    while time.monotonic() < deadline:
        queued = await _task(client, ws, second["id"])
        task_meta = queued["task"].get("metadata_json")
        queue_meta = task_meta.get("queue") if isinstance(task_meta, dict) else None
        if (
            queued["task"]["state"] == "queued"
            and isinstance(queue_meta, dict)
            and queue_meta.get("reason") == "agent_concurrency"
        ):
            break
        await asyncio.sleep(1.0)
    assert queued["task"]["state"] == "queued", queued["task"]
    assert isinstance(queued["task"].get("metadata_json"), dict), queued["task"]
    assert queued["task"]["metadata_json"].get("queue", {}).get("reason") == "agent_concurrency"
    assert queued["runs"] == [], "queued tasks must not have started a run"
    assert await _audit_actions(client, ws, "task.queued", second["id"])

    # The durability moment: bounce the agent worker while task 1 is parked
    # and task 2 is queued. Both workflows must resume from Temporal history.
    compose("restart", "agent-worker", timeout=180.0)

    # Still parked + queued after the restart; now release the slot.
    first_after_restart = await _task(client, ws, first["id"])
    same_run = next(
        (run for run in first_after_restart["runs"] if run["id"] == parked_run["id"]),
        None,
    )
    assert same_run is not None, first_after_restart
    assert same_run["status"] == "waiting_approval", first_after_restart
    pending_after_restart = await _get(
        client, f"/api/v1/workspaces/{ws}/approvals", status="pending"
    )
    assert any(row["id"] == approval_id for row in pending_after_restart["items"])
    still = await _task(client, ws, second["id"])
    assert still["task"]["state"] == "queued", still["task"]
    assert isinstance(still["task"].get("metadata_json"), dict), still["task"]
    assert still["task"]["metadata_json"].get("queue", {}).get("reason") == "agent_concurrency"
    assert still["runs"] == [], "queued tasks must not have started a run after restart"
    approve = await client.post(
        f"{API_URL}/api/v1/workspaces/{ws}/approvals/{approval_id}/approve",
        headers=_csrf(client),
    )
    assert approve.status_code == 200, approve.text

    first_done = await _wait_task_finished(client, ws, first["id"], budget=180.0)
    assert first_done["task"]["state"] == "completed"
    second_done = await _wait_task_finished(client, ws, second["id"], budget=180.0)
    assert second_done["task"]["state"] == "completed"

    # Admission cleared the queue marker, and the slot handoff is provable
    # from run rows: task 2's run started only after task 1's run finished.
    assert "queue" not in second_done["task"]["metadata_json"]
    first_run = first_done["runs"][0]
    second_run = second_done["runs"][0]
    assert len(second_done["runs"]) == 1
    assert second_run["started_at"] >= first_run["completed_at"], (
        first_run,
        second_run,
    )
