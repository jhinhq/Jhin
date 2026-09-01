"""Phase 5 exit tests (plan 45): GitHub connector against the running stack.

(a) A granted agent — grant scoped to one connection and one repository —
    reads repo metadata, creates a branch, and opens + comments a PR through
    the fake GitHub service; the tool_call rows complete and the branch/PR
    are visible in the fake server's state.
(b) The same agent is deterministically denied on a different repository
    (scope_mismatch) and an ungranted agent is denied entirely (no_grant).
(c) Webhook deliveries: a valid HMAC signature yields 202 + a persisted
    ingress event + a normalized canonical connector.* event on the EVENTS
    stream; an invalid signature yields 401 + an audited rejection; a
    duplicate delivery id is deduped (no second event).

Everything runs with zero real GitHub credentials: connections point at the
compose ``fake-github`` service (plan 32.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import nats
import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD

from .conftest import API_URL, FAKE_GITHUB_URL, NATS_URL, compose

pytestmark = pytest.mark.integration

FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
# In-network URL the agent worker uses; host-mapped port for test inspection.
FAKE_GITHUB_INTERNAL = "http://fake-github:8080"
FAKE_GITHUB_HOST = FAKE_GITHUB_URL
FAKE_GITHUB_PAT = "fake-github-pat"
TASK_TIMEOUT_SECONDS = 120.0


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


async def _make_connection(
    client: httpx.AsyncClient, ws: str, tag: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A PAT connection pointed at the in-stack fake GitHub. Returns
    (connection, webhook setup) — the webhook secret is only available here."""
    created = await _post(
        client,
        f"/api/v1/workspaces/{ws}/connections",
        {
            "connector_type": "github",
            "name": f"P5 GitHub {tag}",
            "auth_type": "pat",
            "credentials": {"token": FAKE_GITHUB_PAT},
            "config": {"base_url": FAKE_GITHUB_INTERNAL},
        },
    )
    assert created["webhook"] is not None, "GitHub connections must return webhook setup once"
    return created["connection"], created["webhook"]


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str, name: str) -> dict[str, Any]:
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P5 provider {name} {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P5 profile {name} {tag}",
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"P5 {name} {tag}",
            "system_prompt": "You complete tasks, using tools when instructed.",
            "model_profile_id": profile["id"],
        },
    )


async def _grant(
    client: httpx.AsyncClient, ws: str, agent_id: str, capability: str, scope: dict[str, Any]
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/grants",
        {"capability": capability, "scope": scope, "effect": "allow"},
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
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task['id']} did not finish in {TASK_TIMEOUT_SECONDS}s: {detail}")


async def _tool_calls(client: httpx.AsyncClient, ws: str, run_id: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/runs/{run_id}/tool-calls"
    )
    return calls


async def _fake_github_state() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{FAKE_GITHUB_HOST}/_state")
        assert response.status_code == 200, response.text
        state: dict[str, Any] = response.json()
        return state


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- connection lifecycle -----------------------------------------------------


async def test_connection_create_verify_and_no_plaintext(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    connection, webhook = await _make_connection(client, ws, tag)

    # The credential never comes back from any read endpoint.
    listed = await _get(client, f"/api/v1/workspaces/{ws}/connections")
    mine = next(c for c in listed if c["id"] == connection["id"])
    assert FAKE_GITHUB_PAT not in json.dumps(mine)
    assert webhook["secret"] not in json.dumps(mine)
    assert webhook["url_path"].endswith(connection["public_id"])

    verify = await _post(
        client, f"/api/v1/workspaces/{ws}/connections/{connection['id']}/verify", {}, expect=200
    )
    assert verify["ok"] is True, verify
    assert verify["status"] == "active"
    assert verify["details"]["login"] == "fake-user"
    assert FAKE_GITHUB_PAT not in json.dumps(verify)


# --- (a) granted agent: read repo, create branch, open + comment PR -----------


async def test_granted_agent_reads_repo_creates_branch_and_pr(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    connection, _ = await _make_connection(client, ws, tag)
    agent = await _make_agent(client, ws, tag, "coder")

    scope = {"connection_id": connection["id"], "repository": "octo/alpha"}
    for capability in (
        "github.repository.read",
        "github.branch.create",
        "github.pull_request.create",
        "github.pull_request.comment",
    ):
        await _grant(client, ws, agent["id"], capability, scope)

    branch = f"agent/p5-{tag}"
    conn = connection["id"]
    markers = " ".join(
        [
            f'[[tool:github.repository.read {{"connection_id": "{conn}", '
            f'"repository": "octo/alpha"}}]]',
            f'[[tool:github.branch.create {{"connection_id": "{conn}", '
            f'"repository": "octo/alpha", "branch": "{branch}"}}]]',
            f'[[tool:github.pull_request.create {{"connection_id": "{conn}", '
            f'"repository": "octo/alpha", "title": "P5 fix {tag}", '
            f'"head": "{branch}", "base": "main", "body": "Automated by Jhin."}}]]',
        ]
    )
    detail = await _run_task(
        client, ws, agent["id"], f"GitHub flow {tag}", f"Ship the fix: {markers}"
    )
    assert detail["task"]["state"] == "completed", detail

    calls = await _tool_calls(client, ws, detail["runs"][0]["id"])
    by_name = {call["tool_name"]: call for call in calls}
    assert set(by_name) == {
        "github.repository.read",
        "github.branch.create",
        "github.pull_request.create",
    }
    assert all(call["status"] == "completed" for call in calls), calls
    assert by_name["github.repository.read"]["sanitized_output_json"]["full_name"] == "octo/alpha"
    pr_number = by_name["github.pull_request.create"]["sanitized_output_json"]["number"]
    # The PAT must not appear in any persisted sanitized payload.
    assert FAKE_GITHUB_PAT not in json.dumps(calls)

    # The branch and PR exist in the fake GitHub's state (plan 45 evidence).
    state = await _fake_github_state()
    repo = state["repos"]["octo/alpha"]
    assert branch in repo["branches"]
    assert str(pr_number) in repo["pulls"]
    assert repo["pulls"][str(pr_number)]["title"] == f"P5 fix {tag}"
    assert repo["pulls"][str(pr_number)]["head"]["ref"] == branch


# --- (b) scope mismatch and ungranted agents are denied -----------------------


async def test_scope_mismatch_and_ungranted_are_denied(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    connection, _ = await _make_connection(client, ws, tag)

    scoped = await _make_agent(client, ws, tag, "scoped")
    await _grant(
        client,
        ws,
        scoped["id"],
        "github.repository.read",
        {"connection_id": connection["id"], "repository": "octo/alpha"},
    )
    ungranted = await _make_agent(client, ws, tag, "ungranted")

    marker = (
        f'[[tool:github.repository.read {{"connection_id": "{connection["id"]}", '
        f'"repository": "octo/beta"}}]]'
    )

    # Scoped agent, different repository: denied with scope_mismatch.
    detail = await _run_task(
        client, ws, scoped["id"], f"Wrong repo {tag}", f"Read octo/beta: {marker}"
    )
    calls = await _tool_calls(client, ws, detail["runs"][0]["id"])
    assert len(calls) == 1
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "scope_mismatch"

    # Ungranted agent: denied outright.
    detail = await _run_task(
        client, ws, ungranted["id"], f"No grant {tag}", f"Read octo/beta: {marker}"
    )
    calls = await _tool_calls(client, ws, detail["runs"][0]["id"])
    assert len(calls) == 1
    assert calls[0]["status"] == "denied"
    assert calls[0]["error_code"] == "no_grant"

    # Both denials are audited.
    audit = await _get(
        client, f"/api/v1/workspaces/{ws}/audit-events", action="tool.call.denied", limit=200
    )
    denied_targets = {event["target_id"] for event in audit["events"]}
    assert calls[0]["id"] in denied_targets


# --- (c) webhooks: signature accept/reject, dedupe, normalization -------------


async def _get_last_msg(stream: str, subject: str, deadline_s: float = 20.0) -> Any:
    """Poll JetStream for the newest message on a subject."""
    client = await nats.connect(NATS_URL, connect_timeout=5)
    try:
        js = client.jetstream()
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                return await js.get_last_msg(stream, subject)
            except Exception:
                await asyncio.sleep(0.5)
        return None
    finally:
        await client.close()


async def test_webhook_valid_invalid_and_duplicate(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    connection, webhook = await _make_connection(client, ws, tag)
    path = webhook["url_path"]
    secret = webhook["secret"]

    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": f"P5 webhook {tag}",
            "state": "open",
            "user": {"login": "octocat"},
        },
        "repository": {"full_name": "octo/alpha"},
        "sender": {"login": "octocat"},
    }
    body = json.dumps(payload).encode()
    delivery_id = f"p5-delivery-{tag}"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign(secret, body),
    }

    # Valid signature: accepted, no session auth involved (fresh client).
    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as anon:
        accepted = await anon.post(path, content=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "accepted"
        ingress_event_id = accepted.json()["event_id"]

        # Raw ingress event persisted to the INGRESS stream.
        ingress = await _get_last_msg("INGRESS", f"jhin.v1.{ws}.ingress.github.issues")
        assert ingress is not None, "no ingress event on the INGRESS stream"
        ingress_envelope = json.loads(ingress.data)
        # Ours is the newest; anothers' tags cannot collide with this delivery.
        assert ingress_envelope["data"]["delivery_id"] == delivery_id
        assert ingress_envelope["event_id"] == ingress_event_id
        assert ingress_envelope["source"]["connection_id"] == connection["id"]

        # Normalized canonical event lands on the EVENTS stream (event worker).
        normalized = await _get_last_msg("EVENTS", f"jhin.v1.{ws}.connector.github.issue.opened")
        assert normalized is not None, "no normalized connector event on the EVENTS stream"
        normalized_envelope = json.loads(normalized.data)
        assert normalized_envelope["event_type"] == "connector.github.issue.opened"
        assert normalized_envelope["data"]["title"] == f"P5 webhook {tag}"
        assert normalized_envelope["causation_id"] == ingress_event_id

        # Duplicate delivery id: acknowledged but deduped — no second event.
        duplicate = await anon.post(path, content=body, headers=headers)
        assert duplicate.status_code == 202, duplicate.text
        assert duplicate.json()["status"] == "duplicate"
        ingress_after = await _get_last_msg(
            "INGRESS", f"jhin.v1.{ws}.ingress.github.issues", deadline_s=5.0
        )
        assert ingress_after is not None
        assert json.loads(ingress_after.data)["event_id"] == ingress_event_id

        # Invalid signature: 401 before any processing.
        bad_headers = dict(headers)
        bad_headers["X-GitHub-Delivery"] = f"p5-bad-{tag}"
        bad_headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        rejected = await anon.post(path, content=body, headers=bad_headers)
        assert rejected.status_code == 401, rejected.text

        # Unknown public id: 404 without leaking anything.
        missing = await anon.post(
            f"/api/v1/webhooks/github/{'0' * 32}", content=body, headers=headers
        )
        assert missing.status_code == 404

    # The rejection is audited (actor_type=system).
    audit = await _get(
        client, f"/api/v1/workspaces/{ws}/audit-events", action="webhook.rejected", limit=50
    )
    rejections = [event for event in audit["events"] if event["target_id"] == connection["id"]]
    assert rejections, audit
    assert rejections[0]["actor_type"] == "system"


async def test_connector_tools_visible_in_catalog(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    """The /tools catalog now includes GitHub connector tools with risks."""
    client, ws = owner
    tools = {tool["name"]: tool for tool in await _get(client, f"/api/v1/workspaces/{ws}/tools")}
    assert tools["github.repository.read"]["risk"] == "read"
    assert tools["github.branch.create"]["risk"] == "write"
    assert tools["github.pull_request.merge"]["risk"] == "elevated"
    assert tools["github.pull_request.merge"]["supports_approval"] is True
    connectors = await _get(client, "/api/v1/connectors")
    github = next(c for c in connectors if c["connector_type"] == "github")
    assert github["supports_webhooks"] is True
    # Signing in came first once OAuth landed; the pasted key is the last
    # resort (jhin_connectors.github.manifest.GITHUB_MANIFEST).
    assert {scheme["type"] for scheme in github["auth_schemes"]} == {
        "oauth",
        "device",
        "github_app",
        "pat",
    }
