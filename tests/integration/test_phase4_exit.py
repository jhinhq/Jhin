"""Phase 4 exit tests (plan 45): tool authorization and durable approvals
against the running compose stack.

(a) The same tool call succeeds for a granted agent and is deterministically
    denied (recorded + audited) for an ungranted agent.
(b) An approval-gated call parks the run (waiting_approval), shows up in the
    approvals inbox, and approve resumes/executes while reject finishes the
    run gracefully with the denial recorded.
(c) Durable wait: the agent worker is restarted while a run is parked on an
    approval; approving afterwards still resumes the run (Temporal signal and
    workflow state survive the worker process).

The fake provider emits a tool call for every ``[[tool:name {...}]]`` marker
found in user messages, so task descriptions drive the tool loop
deterministically. All traffic goes through the containerized API — the same
database and Temporal namespace the agent worker uses.
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

from .conftest import API_URL, compose

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
TASK_TIMEOUT_SECONDS = 90.0
PARK_TIMEOUT_SECONDS = 60.0


@pytest.fixture
async def owner() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Logged-in dev-owner client + workspace id, seeding the stack if needed."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        credentials = {"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
        login = await client.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        workspace_id = login.json()["memberships"][0]["workspace_id"]
        yield client, workspace_id


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


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str, name: str) -> dict[str, Any]:
    """Provider + profile + agent wired to the in-stack fake provider."""
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P4 provider {name} {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P4 profile {name} {tag}",
            "input_cost_micros_per_million": 100_000,
            "output_cost_micros_per_million": 400_000,
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"P4 {name} {tag}",
            "system_prompt": "You complete tasks, using tools when instructed.",
            "model_profile_id": profile["id"],
        },
    )


async def _grant(
    client: httpx.AsyncClient, ws: str, agent_id: str, capability: str
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/grants",
        {"capability": capability, "scope": {}, "effect": "allow"},
    )


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
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task_id} did not finish in {deadline_s}s: {detail}")


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
            pytest.fail(f"task finished instead of parking: {detail}")
        await asyncio.sleep(0.5)
    pytest.fail(f"run never reached waiting_approval in {PARK_TIMEOUT_SECONDS}s: {detail}")


async def _pending_approval_for_task(
    client: httpx.AsyncClient, ws: str, task_id: str
) -> dict[str, Any]:
    inbox = await _get(client, f"/api/v1/workspaces/{ws}/approvals", status="pending", limit=200)
    matches = [item for item in inbox["items"] if item["task_id"] == task_id]
    assert len(matches) == 1, f"expected one pending approval for task {task_id}: {inbox}"
    approval: dict[str, Any] = matches[0]
    return approval


async def _tool_calls(client: httpx.AsyncClient, ws: str, run_id: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls"
    )
    return calls


# --- (a) grant allows, deny-by-default blocks --------------------------------


async def test_same_tool_granted_succeeds_ungranted_denied(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    marker = f'[[tool:system.echo {{"text": "phase4-{tag}"}}]]'

    granted = await _make_agent(client, ws, tag, "granted")
    ungranted = await _make_agent(client, ws, tag, "ungranted")
    await _grant(client, ws, granted["id"], "system.echo")

    granted_task = await _assign(
        client, ws, granted["id"], f"Echo (granted) {tag}", f"Use the echo tool: {marker}"
    )
    ungranted_task = await _assign(
        client, ws, ungranted["id"], f"Echo (ungranted) {tag}", f"Use the echo tool: {marker}"
    )

    # Both tasks complete: authorized calls execute; unauthorized calls are
    # denied and the agent finalizes with the denial as its observation.
    granted_detail = await _wait_for_task(client, ws, granted_task["id"])
    ungranted_detail = await _wait_for_task(client, ws, ungranted_task["id"])
    assert granted_detail["task"]["state"] == "completed", granted_detail
    assert ungranted_detail["task"]["state"] == "completed", ungranted_detail

    # Granted: tool_call row executed with the sanitized input/output persisted.
    granted_calls = await _tool_calls(client, ws, granted_detail["runs"][0]["id"])
    assert len(granted_calls) == 1
    call = granted_calls[0]
    assert call["tool_name"] == "system.echo"
    assert call["status"] == "completed"
    assert call["sanitized_input_json"] == {"text": f"phase4-{tag}"}
    assert call["sanitized_output_json"]["text"] == f"phase4-{tag}"
    assert call["duration_ms"] is not None and call["approval_id"] is None

    # Granted: timeline shows the plan-7.3 node path for the tool roundtrip.
    events = [
        e["event_type"]
        for e in await _get(client, f"/api/v1/workspaces/{ws}/tasks/{granted_task['id']}/timeline")
    ]
    for expected in ("node.policy_check", "node.execute_tool", "node.observe", "tool.call"):
        assert expected in events, events
    assert "node.request_approval" not in events

    # Ungranted: deterministic denial, recorded on the same endpoint.
    denied_calls = await _tool_calls(client, ws, ungranted_detail["runs"][0]["id"])
    assert len(denied_calls) == 1
    assert denied_calls[0]["status"] == "denied"
    assert denied_calls[0]["error_code"] == "no_grant"
    denied_events = [
        e["event_type"]
        for e in await _get(
            client, f"/api/v1/workspaces/{ws}/tasks/{ungranted_task['id']}/timeline"
        )
    ]
    assert "node.policy_check" in denied_events
    assert "node.execute_tool" not in denied_events
    assert "tool.call" in denied_events

    # Audited: the denial is in the append-only audit log with the agent actor.
    audit = await _get(
        client, f"/api/v1/workspaces/{ws}/audit-events", action="tool.call.denied", limit=200
    )
    denied_ids = {e["target_id"] for e in audit["events"]}
    assert denied_calls[0]["id"] in denied_ids
    audit_exec = await _get(
        client, f"/api/v1/workspaces/{ws}/audit-events", action="tool.call.executed", limit=200
    )
    assert granted_calls[0]["id"] in {e["target_id"] for e in audit_exec["events"]}


# --- (b) approval gate: approve resumes, reject finalizes gracefully ---------


async def test_approval_gated_call_approve_resumes_and_executes(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    marker = f'[[tool:system.demo.destructive {{"label": "approve-{tag}"}}]]'

    agent = await _make_agent(client, ws, tag, "approver")
    await _grant(client, ws, agent["id"], "system.demo.destructive")

    task = await _assign(
        client, ws, agent["id"], f"Destructive (approve) {tag}", f"Run it: {marker}"
    )
    run = await _wait_for_parked_run(client, ws, task["id"])

    # The approval is in the inbox with the sanitized payload and risk.
    approval = await _pending_approval_for_task(client, ws, task["id"])
    assert approval["action_type"] == "system.demo.destructive"
    assert approval["status"] == "pending"
    assert approval["action_payload_sanitized"]["risk"] == "destructive"
    assert approval["action_payload_sanitized"]["input"]["label"] == f"approve-{tag}"
    assert approval["agent_name"] == agent["name"]

    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{approval['id']}/approve", {}, expect=200
    )

    detail = await _wait_for_task(client, ws, task["id"])
    assert detail["task"]["state"] == "completed", detail

    calls = await _tool_calls(client, ws, run["id"])
    assert len(calls) == 1
    assert calls[0]["status"] == "completed"
    assert calls[0]["approval_id"] == approval["id"]
    assert calls[0]["sanitized_output_json"]["marker"]  # audit marker row id

    events = [
        e["event_type"]
        for e in await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}/timeline")
    ]
    # node.request_approval is the persisted run event (approval.requested is
    # the NATS notification); approval.approved lands when the run resumes.
    for expected in ("node.request_approval", "approval.approved", "node.execute_tool"):
        assert expected in events, events

    decided = await _get(client, f"/api/v1/workspaces/{ws}/approvals", limit=200)
    match = [item for item in decided["items"] if item["id"] == approval["id"]]
    assert match and match[0]["status"] == "approved"
    assert match[0]["decided_at"] is not None


async def test_approval_gated_call_reject_finishes_gracefully(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    marker = f'[[tool:system.demo.destructive {{"label": "reject-{tag}"}}]]'

    agent = await _make_agent(client, ws, tag, "rejecter")
    await _grant(client, ws, agent["id"], "system.demo.destructive")

    task = await _assign(
        client, ws, agent["id"], f"Destructive (reject) {tag}", f"Run it: {marker}"
    )
    run = await _wait_for_parked_run(client, ws, task["id"])
    approval = await _pending_approval_for_task(client, ws, task["id"])

    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{approval['id']}/reject", {}, expect=200
    )

    # The denial is fed into the loop as an observation; the agent finalizes.
    detail = await _wait_for_task(client, ws, task["id"])
    assert detail["task"]["state"] == "completed", detail

    calls = await _tool_calls(client, ws, run["id"])
    assert len(calls) == 1
    assert calls[0]["status"] == "rejected"
    assert calls[0]["approval_id"] == approval["id"]
    assert calls[0]["error_code"] == "approval_rejected"
    assert calls[0]["sanitized_output_json"] == {}

    events = [
        e["event_type"]
        for e in await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}/timeline")
    ]
    assert "approval.rejected" in events, events
    assert "node.execute_tool" not in events


# --- (c) durable wait: worker restart while parked ---------------------------


async def test_durable_approval_wait_survives_agent_worker_restart(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    marker = f'[[tool:system.demo.destructive {{"label": "durable-{tag}"}}]]'

    agent = await _make_agent(client, ws, tag, "durable")
    await _grant(client, ws, agent["id"], "system.demo.destructive")

    task = await _assign(
        client, ws, agent["id"], f"Destructive (durable) {tag}", f"Run it: {marker}"
    )
    run = await _wait_for_parked_run(client, ws, task["id"])
    approval = await _pending_approval_for_task(client, ws, task["id"])

    # Restart the agent worker while the workflow is parked on the approval
    # signal. Temporal owns the wait, so the workflow state must survive.
    compose("restart", "agent-worker", timeout=180.0)

    # Still parked and still in the inbox after the restart.
    detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task['id']}")
    assert detail["runs"][0]["status"] == "waiting_approval", detail
    approval_again = await _pending_approval_for_task(client, ws, task["id"])
    assert approval_again["id"] == approval["id"]

    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{approval['id']}/approve", {}, expect=200
    )

    # The restarted worker resumes the run and executes the tool.
    detail = await _wait_for_task(client, ws, task["id"], deadline_s=120.0)
    assert detail["task"]["state"] == "completed", detail

    calls = await _tool_calls(client, ws, run["id"])
    assert len(calls) == 1
    assert calls[0]["status"] == "completed"
    assert calls[0]["sanitized_output_json"]["marker"]  # audit marker row id
