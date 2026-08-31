"""Phase 9 exit: governed Vercel and Supabase production integrations.

These tests deliberately cross the public API, Temporal agent workflow, durable
ToolGateway, connector HTTP clients, event backbone, and isolated PostgreSQL
fixture.  Focused connector/database suites exercise the exhaustive hostile
matrices; this file proves those boundaries remain joined correctly in the
running product.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import nats
import pytest

from jhin_api.security.passwords import hash_password
from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD
from jhin_events.envelope import EventEnvelope
from jhin_events.streams import EVENTS_STREAM
from jhin_events.subjects import event_subject

from .conftest import (
    API_URL,
    FAKE_SUPABASE_URL,
    FAKE_VERCEL_URL,
    NATS_URL,
    PHASE9_DB_ADMIN_DSN,
    PHASE9_DB_READER_DSN,
    POSTGRES_HOST,
    POSTGRES_PORT,
    WEB_URL,
    compose,
)

pytestmark = pytest.mark.integration

FAKE_PROVIDER_INTERNAL = "http://fake-provider:8080/v1"
FAKE_VERCEL_INTERNAL = "http://fake-vercel:8080"
FAKE_SUPABASE_INTERNAL = "http://fake-supabase:8080"
FAKE_SUPABASE_DB_INTERNAL = "fake-supabase-db:5432"
VERCEL_TOKEN = "fake-vercel-token"
SUPABASE_TOKEN = "fake-supabase-token"
SUPABASE_PROJECT_REF = "abcdefghijklmnopqrst"
WEBHOOK_SECRET = "phase9-vercel-webhook-secret"
TASK_TIMEOUT_SECONDS = 180.0
PARK_TIMEOUT_SECONDS = 60.0
QUIET_SECONDS = 8.0


@dataclass(frozen=True)
class ParkedCall:
    task_id: str
    run_id: str
    approval_id: str
    call_id: str
    risk: str


@pytest.fixture
async def owner_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        credentials = {"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
        login = await client.post("/api/v1/auth/login", json=credentials)
        if login.status_code != 200:
            compose("run", "--rm", "--no-deps", "api", "jhin-seed-dev")
            login = await client.post("/api/v1/auth/login", json=credentials)
        assert login.status_code == 200, login.text
        yield client


def _csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("jhin_csrf")
    assert token
    return {"x-csrf-token": token}


async def _post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    expect: int = 201,
) -> dict[str, Any]:
    response = await client.post(path, json=body or {}, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    if response.status_code == 204:
        return {}
    payload: dict[str, Any] = response.json()
    return payload


async def _put(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    expect: int = 200,
) -> dict[str, Any]:
    response = await client.put(path, json=body, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"
    if response.status_code == 204:
        return {}
    payload: dict[str, Any] = response.json()
    return payload


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params or None)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    return response.json()


async def _delete(client: httpx.AsyncClient, path: str, *, expect: int = 204) -> None:
    response = await client.delete(path, headers=_csrf(client))
    assert response.status_code == expect, f"{path}: {response.status_code} {response.text}"


async def _workspace(client: httpx.AsyncClient, label: str) -> str:
    result = await _post(
        client,
        "/api/v1/workspaces",
        {"name": f"Phase 9 {label} {uuid4().hex[:10]}"},
    )
    return str(result["id"])


async def _create_agent(
    client: httpx.AsyncClient,
    workspace_id: str,
    label: str,
    *,
    preset: str = "balanced",
    custom_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tag = uuid4().hex[:8]
    provider = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"P9 provider {label} {tag}",
            "base_url": FAKE_PROVIDER_INTERNAL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"P9 profile {label} {tag}",
        },
    )
    agent = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents",
        {
            "name": f"P9 {label} {tag}",
            "system_prompt": "Use each explicitly requested tool exactly once.",
            "model_profile_id": profile["id"],
        },
    )
    policy_body = {"rules": custom_rules} if custom_rules is not None else {"preset": preset}
    updated = await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/policy",
        policy_body,
    )
    current = await _get(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/policy",
    )
    assert current == updated
    if custom_rules is None:
        assert current["preset"] == preset
    else:
        assert current["preset"] is None
        assert current["rules"] == custom_rules
    return agent


async def _grant(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
    capability: str,
    scope: dict[str, str],
    *,
    effect: str = "allow",
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent_id}/grants",
        {"capability": capability, "scope": scope, "effect": effect},
    )


async def _vercel_connection(client: httpx.AsyncClient, workspace_id: str) -> dict[str, Any]:
    created = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections",
        {
            "connector_type": "vercel",
            "name": f"Vercel {uuid4().hex[:10]}",
            "auth_type": "access_token",
            "credentials": {"token": VERCEL_TOKEN},
            "config": {"base_url": FAKE_VERCEL_INTERNAL},
        },
    )
    connection: dict[str, Any] = created["connection"]
    assert created["webhook"]["secret_mode"] == "provider_supplied"
    assert created["webhook"]["secret"] is None
    verified = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection['id']}/verify",
        expect=200,
    )
    assert verified["ok"] is True, verified
    assert VERCEL_TOKEN not in json.dumps(created)
    return connection


async def _management_connection(client: httpx.AsyncClient, workspace_id: str) -> dict[str, Any]:
    created = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections",
        {
            "connector_type": "supabase",
            "name": f"Supabase management {uuid4().hex[:10]}",
            "auth_type": "management_token",
            "credentials": {"access_token": SUPABASE_TOKEN},
            "config": {
                "project_ref": SUPABASE_PROJECT_REF,
                "base_url": FAKE_SUPABASE_INTERNAL,
            },
        },
    )
    connection: dict[str, Any] = created["connection"]
    verified = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection['id']}/verify",
        expect=200,
    )
    assert verified["ok"] is True, verified
    assert SUPABASE_TOKEN not in json.dumps(created)
    return connection


def _internal_database_dsn(*, writer: bool) -> str:
    role = "jhin_writer" if writer else "jhin_reader"
    password = "writer-pass" if writer else "reader-pass"
    return f"postgresql://{role}:{password}@{FAKE_SUPABASE_DB_INTERNAL}/supabase_fixture"


async def _database_connection(
    client: httpx.AsyncClient,
    workspace_id: str,
    *,
    writer: bool,
    allow_writes: bool | None = None,
    max_rows: int = 10,
    statement_timeout_ms: int = 1_000,
) -> dict[str, Any]:
    if allow_writes is None:
        allow_writes = writer
    created = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections",
        {
            "connector_type": "supabase",
            "name": f"Supabase database {uuid4().hex[:10]}",
            "auth_type": "postgres",
            "credentials": {"database_url": _internal_database_dsn(writer=writer)},
            "config": {
                "project_ref": SUPABASE_PROJECT_REF,
                "allowed_schemas": ["public"],
                "allow_writes": allow_writes,
                "statement_timeout_ms": statement_timeout_ms,
                "lock_timeout_ms": 500,
                "max_rows": max_rows,
                "max_cell_bytes": 256,
                "max_result_bytes": 4_096,
            },
        },
    )
    connection: dict[str, Any] = created["connection"]
    verified = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection['id']}/verify",
        expect=200,
    )
    assert verified["ok"] is True, verified
    assert "pass" not in json.dumps(created).casefold()
    return connection


def _marker(tool_name: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"[[tool:{tool_name} {encoded}]]"


async def _assign(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
    tool_name: str,
    payload: dict[str, Any],
    *,
    label: str = "tool",
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent_id}/assign-task",
        {
            "title": f"Phase 9 {label} {uuid4().hex[:8]}",
            "description": f"Invoke exactly this tool once: {_marker(tool_name, payload)}",
        },
    )


async def _task(client: httpx.AsyncClient, workspace_id: str, task_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await _get(
        client, f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}"
    )
    return result


async def _wait_task(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
    *,
    budget: float = TASK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + budget
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _task(client, workspace_id, task_id)
        if detail["task"]["state"] in {"completed", "failed", "cancelled"}:
            return detail
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task_id} did not finish: {detail}")


async def _calls(client: httpx.AsyncClient, workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/tool-calls"
    )
    return rows


async def _run(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
    tool_name: str,
    payload: dict[str, Any],
    *,
    label: str = "tool",
) -> tuple[dict[str, Any], dict[str, Any]]:
    assigned = await _assign(client, workspace_id, agent_id, tool_name, payload, label=label)
    detail = await _wait_task(client, workspace_id, str(assigned["id"]))
    assert detail["runs"], detail
    calls = await _calls(client, workspace_id, str(detail["runs"][0]["id"]))
    assert len(calls) == 1, calls
    return detail, calls[0]


async def _wait_parked(
    client: httpx.AsyncClient,
    workspace_id: str,
    task_id: str,
) -> ParkedCall:
    deadline = time.monotonic() + PARK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        detail = await _task(client, workspace_id, task_id)
        pending = await _get(
            client,
            f"/api/v1/workspaces/{workspace_id}/approvals",
            status="pending",
            limit=100,
        )
        for approval in pending["items"]:
            if str(approval.get("task_id")) != task_id:
                continue
            assert detail["runs"], detail
            run_id = str(detail["runs"][0]["id"])
            calls = await _calls(client, workspace_id, run_id)
            waiting = [row for row in calls if row["status"] == "pending_approval"]
            if len(waiting) == 1:
                action = approval.get("action_payload_sanitized")
                assert isinstance(action, dict), approval
                risk = action.get("risk")
                assert isinstance(risk, str), approval
                return ParkedCall(
                    task_id=task_id,
                    run_id=run_id,
                    approval_id=str(approval["id"]),
                    call_id=str(waiting[0]["id"]),
                    risk=risk,
                )
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task_id} did not park for approval")


async def _park(
    client: httpx.AsyncClient,
    workspace_id: str,
    agent_id: str,
    tool_name: str,
    payload: dict[str, Any],
    *,
    label: str = "approval",
) -> ParkedCall:
    assigned = await _assign(client, workspace_id, agent_id, tool_name, payload, label=label)
    return await _wait_parked(client, workspace_id, str(assigned["id"]))


async def _decide(
    client: httpx.AsyncClient,
    workspace_id: str,
    parked: ParkedCall,
    *,
    decision: str = "approve",
) -> tuple[dict[str, Any], dict[str, Any]]:
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/approvals/{parked.approval_id}/{decision}",
        headers=_csrf(client),
    )
    assert response.status_code == 200, response.text
    detail = await _wait_task(client, workspace_id, parked.task_id)
    calls = await _calls(client, workspace_id, parked.run_id)
    exact = [row for row in calls if row["id"] == parked.call_id]
    assert len(exact) == 1, calls
    return detail, exact[0]


async def _race_approve(
    client: httpx.AsyncClient,
    workspace_id: str,
    parked: ParkedCall,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # This exercises the public decision/idempotent-signal race. The separate
    # real-PostgreSQL integration test
    # test_approved_invocation_race_executes_once_and_replays drives two
    # ToolGateway resolvers and proves exactly one GatewayOutcome is replayed.
    path = f"/api/v1/workspaces/{workspace_id}/approvals/{parked.approval_id}/approve"
    first, second = await asyncio.gather(
        client.post(path, headers=_csrf(client)),
        client.post(path, headers=_csrf(client)),
    )
    statuses = sorted((first.status_code, second.status_code))
    assert statuses in ([200, 200], [200, 409]), (first.text, second.text)
    if statuses == [200, 409]:
        conflict = first if first.status_code == 409 else second
        assert "recorded" in conflict.text.casefold(), conflict.text
    detail = await _wait_task(client, workspace_id, parked.task_id)
    calls = await _calls(client, workspace_id, parked.run_id)
    exact = [row for row in calls if row["id"] == parked.call_id]
    assert len(exact) == 1, calls
    return detail, exact[0]


async def _fake_post(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{base_url}{path}", json=body)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _fake_state(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{base_url}/_state")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _wait_canonical_event(
    workspace_id: str,
    event_type: str,
    *,
    budget: float = 30.0,
) -> EventEnvelope:
    connection = await nats.connect(NATS_URL, connect_timeout=5)
    try:
        jetstream = connection.jetstream()
        subject = event_subject(workspace_id, event_type)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                message = await jetstream.get_last_msg(EVENTS_STREAM, subject)
            except Exception:
                await asyncio.sleep(0.25)
                continue
            assert message.data is not None
            return EventEnvelope.from_bytes(message.data)
    finally:
        await connection.close()
    pytest.fail(f"no canonical {event_type} event for {workspace_id}")


async def _wait_trigger_invocations(
    client: httpx.AsyncClient,
    workspace_id: str,
    trigger_id: str,
    count: int,
    *,
    budget: float = 60.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + budget
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await _get(
            client,
            f"/api/v1/workspaces/{workspace_id}/triggers/{trigger_id}/invocations",
        )
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.5)
    pytest.fail(f"trigger {trigger_id} produced {len(rows)}, expected {count}")


async def _audit(
    client: httpx.AsyncClient,
    workspace_id: str,
    action: str,
) -> list[dict[str, Any]]:
    page = await _get(
        client,
        f"/api/v1/workspaces/{workspace_id}/audit-events",
        action=action,
        limit=200,
    )
    events: list[dict[str, Any]] = page["events"]
    return events


def _assert_no_secrets(value: Any, extra: Iterable[str] = ()) -> None:
    encoded = json.dumps(value, sort_keys=True, default=str)
    forbidden = {
        VERCEL_TOKEN,
        SUPABASE_TOKEN,
        WEBHOOK_SECRET,
        "must-never-leak",
        "encrypted-must-never-leak",
        "deployment-secret",
        "event-secret",
        "internal-must-never-leak",
        "unknown-provider-secret",
        "provider-project-secret",
        "project-provider-secret",
        "database-provider-secret",
        "log-provider-secret",
        "function-provider-secret",
        "user-secret",
        "source-secret-marker",
        "private fixture value",
        "reader-pass",
        "writer-pass",
        *extra,
    }
    for marker in forbidden:
        assert marker not in encoded


def _vercel_webhook_body(delivery_id: str, size: int) -> bytes:
    payload: dict[str, Any] = {
        "id": delivery_id,
        "type": "deployment.ready",
        "createdAt": 1_700_000_000_000,
        "payload": {
            "deployment": {
                "id": "dpl_preview",
                "url": "dpl-preview.fake.vercel.app",
                "name": "github-project",
                "meta": {
                    "githubCommitRef": "main",
                    "githubCommitSha": "abc123",
                    "token": "provider-token-must-not-survive",
                },
            },
            "project": {"id": "prj_github"},
            "target": "preview",
            "environment": {"DATABASE_URL": "must-never-leak"},
            "padding": "",
        },
    }
    compact = json.dumps(payload, separators=(",", ":")).encode()
    missing = size - len(compact)
    assert missing >= 0
    payload["payload"]["padding"] = "x" * missing
    exact = json.dumps(payload, separators=(",", ":")).encode()
    assert len(exact) == size
    return exact


async def _fixture_value(dsn: str, sql: str, *params: Any) -> Any:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(sql, *params)
    finally:
        await connection.close()


async def _fixture_execute(dsn: str, sql: str, *params: Any) -> str:
    connection = await asyncpg.connect(dsn)
    try:
        result = await connection.execute(sql, *params)
        return str(result)
    finally:
        await connection.close()


def _app_database_dsn() -> str:
    return f"postgresql://jhin:jhin@{POSTGRES_HOST}:{POSTGRES_PORT}/jhin"


async def _app_execute(sql: str, *params: Any) -> str:
    connection = await asyncpg.connect(_app_database_dsn())
    try:
        result = await connection.execute(sql, *params)
        return str(result)
    finally:
        await connection.close()


async def test_vercel_inspect_is_scoped_bounded_and_display_safe(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Vercel inspect")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    connection = await _vercel_connection(client, workspace_id)
    agent = await _create_agent(client, workspace_id, "Vercel inspector")
    connection_id = str(connection["id"])

    grants = {
        "vercel.project.list": {"connection_id": connection_id},
        "vercel.project.read": {
            "connection_id": connection_id,
            "project_id": "prj_github",
        },
        "vercel.deployment.list": {
            "connection_id": connection_id,
            "project_id": "prj_github",
        },
        "vercel.deployment.read": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_preview",
        },
        "vercel.deployment.logs.read": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_preview",
        },
        "vercel.environment_metadata.read": {
            "connection_id": connection_id,
            "project_id": "prj_github",
        },
    }
    for capability, scope in grants.items():
        await _grant(client, workspace_id, str(agent["id"]), capability, scope)

    read_cases: list[tuple[str, dict[str, Any]]] = [
        ("vercel.project.list", {"connection_id": connection_id, "limit": 10}),
        (
            "vercel.project.read",
            {"connection_id": connection_id, "project_id": "prj_github"},
        ),
        (
            "vercel.deployment.read",
            {
                "connection_id": connection_id,
                "project_id": "prj_github",
                "deployment_id": "dpl_preview",
            },
        ),
        (
            "vercel.deployment.logs.read",
            {
                "connection_id": connection_id,
                "project_id": "prj_github",
                "deployment_id": "dpl_preview",
                "limit": 40,
            },
        ),
        (
            "vercel.environment_metadata.read",
            {"connection_id": connection_id, "project_id": "prj_github"},
        ),
    ]
    outputs: list[dict[str, Any]] = []
    for tool_name, payload in read_cases:
        _, call = await _run(
            client,
            workspace_id,
            str(agent["id"]),
            tool_name,
            payload,
            label=tool_name,
        )
        assert call["status"] == "completed", call
        outputs.append(call["sanitized_output_json"])

    logs = outputs[3]
    assert len(logs["events"]) <= 40
    assert len(json.dumps(logs).encode()) < 32_768
    metadata = outputs[4]
    assert metadata["variables"][0]["key"] == "DATABASE_URL"
    _assert_no_secrets(outputs)

    # A 242-row dataset takes exactly two bounded pages for a requested 200
    # rows. Every provider request must carry the explicit project filter.
    armed = await _fake_post(
        FAKE_VERCEL_URL,
        "/_scenario",
        {"scenario": "deployment_list_pagination"},
    )
    assert armed == {"armed": "deployment_list_pagination"}
    _, page_call = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.list",
        {"connection_id": connection_id, "project_id": "prj_github", "limit": 200},
        label="bounded pagination",
    )
    assert page_call["status"] == "completed", page_call
    output = page_call["sanitized_output_json"]
    assert len(output["deployments"]) == 200
    assert output["truncated"] is True
    assert {row["project_id"] for row in output["deployments"]} == {"prj_github"}
    state = await _fake_state(FAKE_VERCEL_URL)
    page_requests = [
        row
        for row in state["requests"]
        if row["method"] == "GET" and row["path"] == "/v6/deployments"
    ]
    assert len(page_requests) == 2, page_requests
    assert all(row["query"].get("projectId") == ["prj_github"] for row in page_requests)

    # Validate every row before returning any: one cross-project provider row
    # makes the whole result fail closed.
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    await _fake_post(
        FAKE_VERCEL_URL,
        "/_scenario",
        {"scenario": "deployment_list_mixed_project"},
    )
    _, mixed = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.list",
        {"connection_id": connection_id, "project_id": "prj_github", "limit": 20},
        label="mixed project rejection",
    )
    assert mixed["status"] == "failed", mixed
    assert mixed["error_code"] == "project_scope_mismatch"
    assert mixed["sanitized_output_json"] == {"error": "project_scope_mismatch"}


async def test_vercel_mutations_require_exact_scope_and_balanced_approval(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Vercel release guard")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    connection = await _vercel_connection(client, workspace_id)
    connection_id = str(connection["id"])
    preview_payload = {
        "connection_id": connection_id,
        "project_id": "prj_github",
        "environment": "preview",
        "git_provider": "github",
        "repository_id": "101",
        "ref": "main",
    }

    no_grant = await _create_agent(client, workspace_id, "No Vercel grant")
    _, denied = await _run(
        client,
        workspace_id,
        str(no_grant["id"]),
        "vercel.deployment.preview.create",
        preview_payload,
        label="preview without grant",
    )
    assert denied["status"] == "denied"
    assert denied["error_code"] == "no_grant"

    unscoped = await _create_agent(client, workspace_id, "Unscoped Vercel grant")
    await _grant(
        client,
        workspace_id,
        str(unscoped["id"]),
        "vercel.deployment.preview.create",
        {},
    )
    _, unscoped_call = await _run(
        client,
        workspace_id,
        str(unscoped["id"]),
        "vercel.deployment.preview.create",
        preview_payload,
        label="preview with incomplete grant",
    )
    assert unscoped_call["status"] == "denied"
    assert unscoped_call["error_code"] == "required_scope_missing"
    assert (await _fake_state(FAKE_VERCEL_URL))["counters"]["preview_create"] == 0

    agent = await _create_agent(client, workspace_id, "Vercel release agent")
    exact_scopes = {
        "vercel.deployment.preview.create": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "environment": "preview",
            "repository_id": "101",
        },
        "vercel.deployment.redeploy": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_preview",
            "environment": "preview",
        },
        "vercel.deployment.promote": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_preview",
            "environment": "production",
        },
        "vercel.deployment.alias.assign": {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_production",
            "environment": "production",
            "alias": "phase9.example.com",
        },
    }
    for capability, scope in exact_scopes.items():
        await _grant(client, workspace_id, str(agent["id"]), capability, scope)

    wrong_project = {**preview_payload, "project_id": "prj_other"}
    _, wrong_project_call = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        wrong_project,
        label="wrong Vercel project scope",
    )
    assert wrong_project_call["status"] == "denied"
    assert wrong_project_call["error_code"] == "scope_mismatch"

    # These grants match the submitted inputs, deliberately reaching the
    # provider-ownership rechecks while still producing zero mutations.
    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        {
            "connection_id": connection_id,
            "project_id": "prj_gitlab",
            "environment": "preview",
            "repository_id": "gl-project-202",
        },
    )
    provider_mismatch = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        {
            "connection_id": connection_id,
            "project_id": "prj_gitlab",
            "environment": "preview",
            "git_provider": "github",
            "repository_id": "gl-project-202",
            "ref": "main",
        },
        label="mismatched project Git provider link",
    )
    assert provider_mismatch.risk == "elevated"
    _, provider_mismatch_call = await _decide(client, workspace_id, provider_mismatch)
    assert provider_mismatch_call["status"] == "failed", provider_mismatch_call
    assert provider_mismatch_call["error_code"] == "repository_scope_mismatch"

    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_other",
            "environment": "preview",
        },
    )
    ownership = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        {
            "connection_id": connection_id,
            "project_id": "prj_github",
            "deployment_id": "dpl_other",
            "environment": "preview",
        },
        label="wrong deployment ownership",
    )
    assert ownership.risk == "destructive"
    _, ownership_call = await _decide(client, workspace_id, ownership)
    assert ownership_call["status"] == "failed", ownership_call
    assert ownership_call["error_code"] == "project_scope_mismatch"

    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        {
            "connection_id": connection_id,
            "project_id": "prj_other",
            "environment": "preview",
            "repository_id": "101",
        },
    )
    git_mismatch = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        {
            **preview_payload,
            "project_id": "prj_other",
            "repository_id": "101",
        },
        label="wrong repository for linked project",
    )
    assert git_mismatch.risk == "elevated"
    _, git_call = await _decide(client, workspace_id, git_mismatch)
    assert git_call["status"] == "failed", git_call
    assert git_call["error_code"] == "repository_scope_mismatch"
    assert (await _fake_state(FAKE_VERCEL_URL))["counters"] == {
        "preview_create": 0,
        "redeploy": 0,
        "promote": 0,
        "alias": 0,
    }
    _assert_no_secrets([provider_mismatch_call, ownership_call, git_call])

    missing = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.preview.create",
        preview_payload,
        label="missing Balanced approval",
    )
    assert missing.risk == "elevated"
    assert (await _fake_state(FAKE_VERCEL_URL))["counters"]["preview_create"] == 0
    _, rejected = await _decide(client, workspace_id, missing, decision="reject")
    assert rejected["status"] == "rejected"

    approved_cases = [
        ("vercel.deployment.preview.create", preview_payload, "preview_create", "elevated"),
        (
            "vercel.deployment.redeploy",
            {
                "connection_id": connection_id,
                "project_id": "prj_github",
                "deployment_id": "dpl_preview",
                "environment": "preview",
            },
            "redeploy",
            "destructive",
        ),
        (
            "vercel.deployment.promote",
            {
                "connection_id": connection_id,
                "project_id": "prj_github",
                "deployment_id": "dpl_preview",
                "environment": "production",
            },
            "promote",
            "destructive",
        ),
        (
            "vercel.deployment.alias.assign",
            {
                "connection_id": connection_id,
                "project_id": "prj_github",
                "deployment_id": "dpl_production",
                "environment": "production",
                "alias": "phase9.example.com",
            },
            "alias",
            "destructive",
        ),
    ]
    for tool_name, payload, counter, expected_risk in approved_cases:
        parked = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            tool_name,
            payload,
            label=f"approved {counter}",
        )
        assert parked.risk == expected_risk
        _, call = await _decide(client, workspace_id, parked)
        assert call["status"] == "completed", call
        assert (await _fake_state(FAKE_VERCEL_URL))["counters"][counter] == 1
        _assert_no_secrets(call)
    assert await _audit(client, workspace_id, "tool.call.denied")


async def test_parked_approvals_recheck_grant_policy_connection_and_definition(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Approval liveness")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    connection = await _vercel_connection(client, workspace_id)
    connection_id = str(connection["id"])
    agent = await _create_agent(client, workspace_id, "Approval liveness")
    payload = {
        "connection_id": connection_id,
        "project_id": "prj_github",
        "deployment_id": "dpl_preview",
        "environment": "preview",
    }
    scope = dict(payload)

    grant = await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        scope,
    )
    revoked = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="grant revoked after park",
    )
    await _delete(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/grants/{grant['id']}",
    )
    _, revoked_call = await _decide(client, workspace_id, revoked)
    assert revoked_call["status"] == "denied", revoked_call
    assert revoked_call["error_code"] in {"no_grant", "scope_mismatch"}

    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        scope,
    )
    forbidden = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="policy forbidden after park",
    )
    await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/policy",
        {
            "rules": [
                {
                    "capability": "vercel.deployment.redeploy",
                    "action": "forbid",
                }
            ]
        },
    )
    _, forbidden_call = await _decide(client, workspace_id, forbidden)
    assert forbidden_call["status"] == "denied", forbidden_call
    assert forbidden_call["error_code"] == "forbidden_by_policy"
    restored = await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/agents/{agent['id']}/policy",
        {"preset": "balanced"},
    )
    assert restored["preset"] == "balanced"

    rotated = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="credential rotated after park",
    )
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
        {"credentials": {"token": "rotated-invalid-vercel-token"}},
        expect=200,
    )
    _, rotated_call = await _decide(client, workspace_id, rotated)
    assert rotated_call["status"] == "denied", rotated_call
    assert rotated_call["error_code"] == "approval_connection_changed"
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
        {"credentials": {"token": VERCEL_TOKEN}},
        expect=200,
    )

    config_drift = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="public config changed after park",
    )
    await _app_execute(
        "UPDATE connection SET config_json = jsonb_set(config_json, '{team_id}', "
        "'\"team_drift\"'::jsonb) WHERE id::text = $1",
        connection_id,
    )
    _, config_call = await _decide(client, workspace_id, config_drift)
    assert config_call["status"] == "denied", config_call
    assert config_call["error_code"] == "approval_connection_changed"
    await _app_execute(
        "UPDATE connection SET config_json = config_json - 'team_id' WHERE id::text = $1",
        connection_id,
    )

    disabled = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="connection disabled after park",
    )
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/disable",
        expect=200,
    )
    _, disabled_call = await _decide(client, workspace_id, disabled)
    assert disabled_call["status"] == "denied", disabled_call
    assert disabled_call["error_code"] in {
        "approval_connection_changed",
        "connection_disabled",
    }
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/enable",
        expect=200,
    )

    definition = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        payload,
        label="definition changed after park",
    )
    await _app_execute(
        "UPDATE approval SET action_payload_sanitized = "
        "jsonb_set(action_payload_sanitized, '{risk}', '\"read\"'::jsonb) "
        "WHERE id::text = $1",
        definition.approval_id,
    )
    _, definition_call = await _decide(client, workspace_id, definition)
    assert definition_call["status"] == "failed", definition_call
    assert definition_call["error_code"] == "approval_definition_changed"

    deleted_connection = await _vercel_connection(client, workspace_id)
    deleted_id = str(deleted_connection["id"])
    deleted_scope = {
        **scope,
        "connection_id": deleted_id,
    }
    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        deleted_scope,
    )
    deleted = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "vercel.deployment.redeploy",
        deleted_scope,
        label="connection deleted after park",
    )
    await _delete(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{deleted_id}",
    )
    _, deleted_call = await _decide(client, workspace_id, deleted)
    assert deleted_call["status"] == "denied", deleted_call
    assert deleted_call["error_code"] in {
        "approval_connection_changed",
        "approval_connection_unavailable",
    }

    sql_table = f"phase9_approval_liveness_{uuid4().hex[:10]}"
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'CREATE TABLE public."{sql_table}" (id integer PRIMARY KEY, value text NOT NULL)',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'ALTER TABLE public."{sql_table}" ALTER COLUMN value SET STORAGE EXTERNAL',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public."{sql_table}" TO jhin_writer',
    )
    try:
        database = await _database_connection(client, workspace_id, writer=True)
        database_id = str(database["id"])
        database_scope = {
            "connection_id": database_id,
            "project_ref": SUPABASE_PROJECT_REF,
            "schema": "public",
        }
        await _grant(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            database_scope,
        )
        sql_drift = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **database_scope,
                "sql": f'INSERT INTO public."{sql_table}" (id, value) VALUES ($1, $2)',
                "params": [1, "must-not-land"],
            },
            label="database credential changed after park",
        )
        assert sql_drift.risk == "elevated"
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/connections/{database_id}/rotate",
            {"credentials": {"database_url": _internal_database_dsn(writer=False)}},
            expect=200,
        )
        _, sql_drift_call = await _decide(client, workspace_id, sql_drift)
        assert sql_drift_call["status"] == "denied", sql_drift_call
        assert sql_drift_call["error_code"] == "approval_connection_changed"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{sql_table}"',
            )
            == 0
        )
    finally:
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f'DROP TABLE IF EXISTS public."{sql_table}"',
        )

    state = await _fake_state(FAKE_VERCEL_URL)
    assert state["counters"]["redeploy"] == 0
    denial_codes = {
        event["target_id"]: event["metadata_json"].get("code")
        for event in await _audit(client, workspace_id, "tool.call.denied")
    }
    expected_denials = {
        revoked_call["id"]: "no_grant",
        forbidden_call["id"]: "forbidden_by_policy",
        rotated_call["id"]: "approval_connection_changed",
        config_call["id"]: "approval_connection_changed",
        disabled_call["id"]: "approval_connection_changed",
        deleted_call["id"]: "approval_connection_changed",
        sql_drift_call["id"]: "approval_connection_changed",
    }
    assert {call_id: denial_codes.get(call_id) for call_id in expected_denials} == expected_denials
    failure_codes = {
        event["target_id"]: event["metadata_json"].get("code")
        for event in await _audit(client, workspace_id, "tool.call.failed")
    }
    assert failure_codes.get(definition_call["id"]) == "approval_definition_changed"
    _assert_no_secrets(
        [
            revoked_call,
            forbidden_call,
            rotated_call,
            config_call,
            disabled_call,
            definition_call,
            deleted_call,
            sql_drift_call,
        ],
        {"rotated-invalid-vercel-token"},
    )


async def test_invocation_races_autonomy_and_post_effect_faults_are_at_most_once(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Invocation races")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    await _fake_post(FAKE_SUPABASE_URL, "/_reset", {})
    vercel = await _vercel_connection(client, workspace_id)
    management = await _management_connection(client, workspace_id)
    vercel_id = str(vercel["id"])
    management_id = str(management["id"])

    balanced = await _create_agent(client, workspace_id, "Racing approvals")
    vercel_scope = {
        "connection_id": vercel_id,
        "project_id": "prj_github",
        "deployment_id": "dpl_preview",
        "environment": "preview",
    }
    function_scope = {
        "connection_id": management_id,
        "project_ref": SUPABASE_PROJECT_REF,
        "function_slug": "race-function",
    }
    await _grant(
        client,
        workspace_id,
        str(balanced["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
    )
    await _grant(
        client,
        workspace_id,
        str(balanced["id"]),
        "supabase.function.deploy",
        function_scope,
    )
    vercel_race = await _park(
        client,
        workspace_id,
        str(balanced["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
        label="Vercel approval race",
    )
    _, vercel_call = await _race_approve(client, workspace_id, vercel_race)
    assert vercel_call["status"] == "completed", vercel_call
    assert (await _fake_state(FAKE_VERCEL_URL))["counters"]["redeploy"] == 1

    deploy_payload = {
        **function_scope,
        "entrypoint_path": "index.ts",
        "verify_jwt": True,
        "files": [{"path": "index.ts", "content": "export default 'race'"}],
    }
    function_race = await _park(
        client,
        workspace_id,
        str(balanced["id"]),
        "supabase.function.deploy",
        deploy_payload,
        label="Supabase approval race",
    )
    _, function_call = await _race_approve(client, workspace_id, function_race)
    assert function_call["status"] == "completed", function_call
    assert (await _fake_state(FAKE_SUPABASE_URL))["counters"]["deploy"] == 1

    autonomous = await _create_agent(
        client, workspace_id, "Autonomous release", preset="autonomous"
    )
    preview_scope = {
        "connection_id": vercel_id,
        "project_id": "prj_github",
        "environment": "preview",
        "repository_id": "101",
    }
    await _grant(
        client,
        workspace_id,
        str(autonomous["id"]),
        "vercel.deployment.preview.create",
        preview_scope,
    )
    await _grant(
        client,
        workspace_id,
        str(autonomous["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
    )
    _, preview_call = await _run(
        client,
        workspace_id,
        str(autonomous["id"]),
        "vercel.deployment.preview.create",
        {
            **preview_scope,
            "git_provider": "github",
            "ref": "agent/phase9",
        },
        label="Autonomous elevated preview",
    )
    assert preview_call["status"] == "completed", preview_call
    destructive = await _park(
        client,
        workspace_id,
        str(autonomous["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
        label="Autonomous destructive still parks",
    )
    _, destructive_rejected = await _decide(client, workspace_id, destructive, decision="reject")
    assert destructive_rejected["status"] == "rejected"

    custom = await _create_agent(
        client,
        workspace_id,
        "Explicit destructive auto",
        custom_rules=[
            {
                "capability": "vercel.deployment.redeploy",
                "risk": "destructive",
                "action": "auto",
            },
            {
                "capability": "supabase.function.deploy",
                "risk": "destructive",
                "action": "auto",
            },
        ],
    )
    await _grant(
        client,
        workspace_id,
        str(custom["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
    )
    fault_function_scope = {
        **function_scope,
        "function_slug": "fault-function",
    }
    await _grant(
        client,
        workspace_id,
        str(custom["id"]),
        "supabase.function.deploy",
        fault_function_scope,
    )

    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    await _fake_post(FAKE_VERCEL_URL, "/_fault", {"mutation": "redeploy"})
    vercel_unknown_detail, vercel_unknown = await _run(
        client,
        workspace_id,
        str(custom["id"]),
        "vercel.deployment.redeploy",
        vercel_scope,
        label="Vercel post-effect ambiguity",
    )
    assert vercel_unknown["status"] == "execution_unknown", vercel_unknown
    assert vercel_unknown_detail["runs"][0]["error_code"] == "tool_execution_unknown"
    assert (await _fake_state(FAKE_VERCEL_URL))["counters"]["redeploy"] == 1

    await _fake_post(FAKE_SUPABASE_URL, "/_reset", {})
    await _fake_post(FAKE_SUPABASE_URL, "/_fault", {"mutation": "deploy"})
    supabase_unknown_detail, supabase_unknown = await _run(
        client,
        workspace_id,
        str(custom["id"]),
        "supabase.function.deploy",
        {
            **fault_function_scope,
            "entrypoint_path": "index.ts",
            "verify_jwt": True,
            "files": [{"path": "index.ts", "content": "export default 'fault'"}],
        },
        label="Supabase post-effect ambiguity",
    )
    assert supabase_unknown["status"] == "execution_unknown", supabase_unknown
    assert supabase_unknown_detail["runs"][0]["error_code"] == "tool_execution_unknown"
    assert (await _fake_state(FAKE_SUPABASE_URL))["counters"]["deploy"] == 1

    for detail, call in (
        (vercel_unknown_detail, vercel_unknown),
        (supabase_unknown_detail, supabase_unknown),
    ):
        task_id = str(detail["task"]["id"])
        # Tool-call/result transcript rows are intentionally internal and are
        # omitted from the public task-message route. Inspect their durable
        # cardinality directly so a hidden duplicate cannot pass as "zero".
        message_count = await _fixture_value(
            _app_database_dsn(),
            "SELECT count(*) FROM message "
            "WHERE task_id = $1::uuid AND message_type = 'tool_result' "
            "AND content_json->>'tool_call_id' = $2 "
            "AND content_json->>'status' = 'execution_unknown'",
            task_id,
            call["id"],
        )
        assert message_count == 1
        timeline = await _get(
            client,
            f"/api/v1/workspaces/{workspace_id}/runs/{detail['runs'][0]['id']}/timeline",
        )
        markers = [
            row
            for row in timeline
            if row["event_type"] == "agent.step.committed"
            and row["payload_json"].get("result", {}).get("execution_unknown_tool_call_id")
            == call["id"]
        ]
        assert len(markers) == 1, timeline

    race_table = f"phase9_race_{uuid4().hex[:10]}"
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'CREATE TABLE public."{race_table}" (id integer PRIMARY KEY, value text NOT NULL)',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'ALTER TABLE public."{race_table}" ALTER COLUMN value SET STORAGE EXTERNAL',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public."{race_table}" TO jhin_writer',
    )
    try:
        database = await _database_connection(client, workspace_id, writer=True)
        database_id = str(database["id"])
        database_agent = await _create_agent(client, workspace_id, "Database race")
        database_scope = {
            "connection_id": database_id,
            "project_ref": SUPABASE_PROJECT_REF,
            "schema": "public",
        }
        await _grant(
            client,
            workspace_id,
            str(database_agent["id"]),
            "supabase.database.write",
            database_scope,
        )
        database_race = await _park(
            client,
            workspace_id,
            str(database_agent["id"]),
            "supabase.database.write",
            {
                **database_scope,
                "sql": f'INSERT INTO public."{race_table}" (id, value) VALUES ($1, $2)',
                "params": [1, "once"],
            },
            label="database approval race",
        )
        _, database_call = await _race_approve(client, workspace_id, database_race)
        assert database_call["status"] == "completed", database_call
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{race_table}" WHERE id = 1',
            )
            == 1
        )
    finally:
        await _fixture_execute(PHASE9_DB_ADMIN_DSN, f'DROP TABLE IF EXISTS public."{race_table}"')
    _assert_no_secrets([vercel_call, function_call, preview_call, vercel_unknown, supabase_unknown])


async def test_vercel_webhook_cap_signature_retry_and_deduplication(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Vercel webhook")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    connection = await _vercel_connection(client, workspace_id)
    connection_id = str(connection["id"])
    await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/webhook-secret",
        {"secret": WEBHOOK_SECRET},
        expect=204,
    )
    agent = await _create_agent(client, workspace_id, "Webhook target")
    trigger = await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/triggers",
        {
            "name": f"Phase 9 Vercel ready {uuid4().hex[:8]}",
            "connection_id": connection_id,
            "event_type": "connector.vercel.deployment.ready",
            "filter": {},
            "target_agent_id": agent["id"],
            "action_config": {},
            "dedupe_window_seconds": 3_600,
        },
    )
    # The trigger matcher cache is intentionally short-lived; settle it before
    # the event so this test measures dedupe, not cache propagation.
    await asyncio.sleep(6.0)

    body = _vercel_webhook_body(f"evt-phase9-{uuid4().hex}", 1_048_576)
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha1).hexdigest()
    path = f"/api/v1/webhooks/vercel/{connection['public_id']}"

    # Inject a deferred database failure: NATS publish succeeds, then the
    # transaction's commit fails. The provider retry must reuse the same IDs.
    suffix = uuid4().hex[:10]
    function_name = f"phase9_fail_webhook_{suffix}"
    trigger_name = f"phase9_fail_webhook_{suffix}"
    await _app_execute(
        f"CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'phase9 injected precommit failure'; END $$"
    )
    await _app_execute(
        f"CREATE CONSTRAINT TRIGGER {trigger_name} AFTER INSERT ON webhook_delivery "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {function_name}()"
    )
    try:
        first = await client.post(
            path,
            content=body,
            headers={"content-type": "application/json", "x-vercel-signature": signature},
        )
        assert first.status_code == 503, first.text
    finally:
        await _app_execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON webhook_delivery")
        await _app_execute(f"DROP FUNCTION IF EXISTS {function_name}()")

    assert (
        await _fixture_value(
            _app_database_dsn(),
            "SELECT count(*) FROM webhook_delivery WHERE connection_id::text = $1",
            connection_id,
        )
        == 0
    )
    retry = await client.post(
        path,
        content=body,
        headers={"content-type": "application/json", "x-vercel-signature": signature},
    )
    assert retry.status_code == 202, retry.text
    replay = await client.post(
        path,
        content=body,
        headers={"content-type": "application/json", "x-vercel-signature": signature},
    )
    assert replay.status_code == 202, replay.text

    bad = await client.post(
        path,
        content=body,
        headers={"content-type": "application/json", "x-vercel-signature": "0" * 40},
    )
    assert bad.status_code == 401, bad.text
    oversized = _vercel_webhook_body(f"evt-over-{uuid4().hex}", 1_048_577)
    over = await client.post(
        path,
        content=oversized,
        headers={
            "content-type": "application/json",
            "x-vercel-signature": hmac.new(
                WEBHOOK_SECRET.encode(), oversized, hashlib.sha1
            ).hexdigest(),
        },
    )
    assert over.status_code == 413, over.text

    app = await asyncpg.connect(_app_database_dsn())
    try:
        row = await app.fetchrow(
            "SELECT event_id::text, delivery_id FROM webhook_delivery "
            "WHERE connection_id::text = $1",
            connection_id,
        )
    finally:
        await app.close()
    assert row is not None
    assert row["delivery_id"].startswith("evt-phase9-")
    ingress_event_id = str(row["event_id"])
    canonical = await _wait_canonical_event(workspace_id, "connector.vercel.deployment.ready")
    assert str(canonical.causation_id) == ingress_event_id
    assert canonical.source.connection_id is not None
    assert str(canonical.source.connection_id) == connection_id
    assert canonical.data["deployment_id"] == "dpl_preview"
    assert canonical.data["project_id"] == "prj_github"
    _assert_no_secrets(canonical.model_dump(mode="json"), {"provider-token-must-not-survive"})

    invocations = await _wait_trigger_invocations(client, workspace_id, str(trigger["id"]), 1)
    await asyncio.sleep(QUIET_SECONDS)
    final_invocations = await _get(
        client,
        f"/api/v1/workspaces/{workspace_id}/triggers/{trigger['id']}/invocations",
    )
    assert len(invocations) == len(final_invocations) == 1
    nats_connection = await nats.connect(NATS_URL, connect_timeout=5)
    canonical_subject = event_subject(
        workspace_id,
        "connector.vercel.deployment.ready",
    )
    try:
        stream = await nats_connection.jetstream().stream_info(
            EVENTS_STREAM,
            subjects_filter=canonical_subject,
        )
    finally:
        await nats_connection.close()
    assert stream.state.subjects == {canonical_subject: 1}
    assert (
        await _fixture_value(
            _app_database_dsn(),
            "SELECT count(*) FROM webhook_delivery WHERE connection_id::text = $1",
            connection_id,
        )
        == 1
    )
    rejection_events = await _audit(client, workspace_id, "webhook.rejected")
    assert any(event["target_id"] == connection_id for event in rejection_events)


async def test_supabase_management_reads_and_mutations_are_project_scoped(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Supabase management")
    await _fake_post(FAKE_SUPABASE_URL, "/_reset", {})
    connection = await _management_connection(client, workspace_id)
    agent = await _create_agent(client, workspace_id, "Supabase manager")
    connection_id = str(connection["id"])
    common = {"connection_id": connection_id, "project_ref": SUPABASE_PROJECT_REF}

    for capability, scope in (
        ("supabase.project.read", common),
        ("supabase.logs.read", {**common, "source": "edge_logs"}),
        ("supabase.function.list", common),
        (
            "supabase.function.deploy",
            {**common, "function_slug": "phase9-function"},
        ),
        (
            "supabase.function.delete",
            {**common, "function_slug": "phase9-function"},
        ),
        (
            "supabase.function.deploy",
            {**common, "function_slug": "pending-function"},
        ),
    ):
        await _grant(client, workspace_id, str(agent["id"]), capability, scope)

    start = datetime(2026, 8, 17, tzinfo=UTC)
    reads: list[tuple[str, dict[str, Any]]] = [
        ("supabase.project.read", common),
        (
            "supabase.logs.read",
            {
                **common,
                "source": "edge_logs",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "limit": 20,
            },
        ),
        ("supabase.function.list", {**common, "limit": 20}),
    ]
    read_outputs: list[dict[str, Any]] = []
    for tool_name, payload in reads:
        _, call = await _run(
            client,
            workspace_id,
            str(agent["id"]),
            tool_name,
            payload,
            label=tool_name,
        )
        assert call["status"] == "completed", call
        read_outputs.append(call["sanitized_output_json"])
    assert read_outputs[0]["project_ref"] == SUPABASE_PROJECT_REF
    assert len(read_outputs[1]["logs"]) <= 20
    assert len(read_outputs[2]["functions"]) <= 20
    _assert_no_secrets(read_outputs)

    # Wrong project and function scopes fail before provider mutation.
    _, wrong_project = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.project.read",
        {"connection_id": connection_id, "project_ref": "wrong-project"},
        label="wrong management project",
    )
    assert wrong_project["status"] == "denied"
    assert wrong_project["error_code"] == "scope_mismatch"
    _, wrong_function = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.function.deploy",
        {
            **common,
            "function_slug": "other-function",
            "entrypoint_path": "index.ts",
            "verify_jwt": True,
            "files": [{"path": "index.ts", "content": "Deno.serve(() => new Response('x'))"}],
        },
        label="wrong function scope",
    )
    assert wrong_function["status"] == "denied"
    assert wrong_function["error_code"] == "scope_mismatch"
    assert (await _fake_state(FAKE_SUPABASE_URL))["counters"] == {
        "deploy": 0,
        "delete": 0,
    }

    # Merely parking a destructive call has no provider effect.
    pending = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.function.deploy",
        {
            **common,
            "function_slug": "pending-function",
            "entrypoint_path": "index.ts",
            "verify_jwt": True,
            "files": [{"path": "index.ts", "content": "export default 'pending'"}],
        },
        label="missing management approval",
    )
    assert pending.risk == "destructive"
    assert (await _fake_state(FAKE_SUPABASE_URL))["counters"]["deploy"] == 0
    _, rejected = await _decide(client, workspace_id, pending, decision="reject")
    assert rejected["status"] == "rejected"

    deploy = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.function.deploy",
        {
            **common,
            "function_slug": "phase9-function",
            "entrypoint_path": "index.ts",
            "verify_jwt": True,
            "files": [
                {
                    "path": "index.ts",
                    "content": "Deno.serve(() => new Response('phase9-source-secret'))",
                }
            ],
        },
        label="approved function deploy",
    )
    assert deploy.risk == "destructive"
    _, deployed = await _decide(client, workspace_id, deploy)
    assert deployed["status"] == "completed", deployed
    assert deployed["sanitized_output_json"]["slug"] == "phase9-function"
    state = await _fake_state(FAKE_SUPABASE_URL)
    assert state["counters"]["deploy"] == 1
    _assert_no_secrets(deployed, {"phase9-source-secret"})

    delete = await _park(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.function.delete",
        {**common, "function_slug": "phase9-function"},
        label="approved function delete",
    )
    assert delete.risk == "destructive"
    _, deleted = await _decide(client, workspace_id, delete)
    assert deleted["status"] == "completed", deleted
    assert deleted["sanitized_output_json"]["deleted"] is True
    state = await _fake_state(FAKE_SUPABASE_URL)
    assert state["counters"] == {"deploy": 1, "delete": 1}
    _assert_no_secrets(state)


async def test_supabase_authority_planes_and_workspaces_cannot_substitute(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Supabase plane isolation")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    await _fake_post(FAKE_SUPABASE_URL, "/_reset", {})
    management = await _management_connection(client, workspace_id)
    database = await _database_connection(client, workspace_id, writer=False)
    agent = await _create_agent(client, workspace_id, "Plane isolation")
    management_id = str(management["id"])
    database_id = str(database["id"])

    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.project.read",
        {"connection_id": database_id, "project_ref": SUPABASE_PROJECT_REF},
    )
    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        {
            "connection_id": management_id,
            "project_ref": SUPABASE_PROJECT_REF,
            "schema": "public",
        },
    )
    _, management_on_database = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.project.read",
        {"connection_id": database_id, "project_ref": SUPABASE_PROJECT_REF},
        label="management tool on postgres authority",
    )
    assert management_on_database["status"] == "failed", management_on_database
    assert management_on_database["error_code"] == "unsupported_auth_type"
    _, database_on_management = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        {
            "connection_id": management_id,
            "project_ref": SUPABASE_PROJECT_REF,
            "schema": "public",
            "sql": "SELECT public.widgets.id FROM public.widgets ORDER BY public.widgets.id",
            "params": [],
        },
        label="database tool on management authority",
    )
    assert database_on_management["status"] == "failed", database_on_management
    assert database_on_management["error_code"] == "unsupported_auth_type"

    foreign_workspace = await _workspace(client, "Foreign workspace")
    foreign_agent = await _create_agent(client, foreign_workspace, "Foreign agent")
    foreign_scope = {
        "connection_id": management_id,
        "project_ref": SUPABASE_PROJECT_REF,
    }
    await _grant(
        client,
        foreign_workspace,
        str(foreign_agent["id"]),
        "supabase.project.read",
        foreign_scope,
    )
    _, foreign_call = await _run(
        client,
        foreign_workspace,
        str(foreign_agent["id"]),
        "supabase.project.read",
        foreign_scope,
        label="foreign workspace connection",
    )
    assert foreign_call["status"] == "failed", foreign_call
    assert foreign_call["error_code"] == "connection_unavailable"
    assert "workspace" not in json.dumps(foreign_call).casefold()

    foreign_database_scope = {
        "connection_id": database_id,
        "project_ref": SUPABASE_PROJECT_REF,
        "schema": "public",
    }
    await _grant(
        client,
        foreign_workspace,
        str(foreign_agent["id"]),
        "supabase.database.read",
        foreign_database_scope,
    )
    _, foreign_database_call = await _run(
        client,
        foreign_workspace,
        str(foreign_agent["id"]),
        "supabase.database.read",
        {
            **foreign_database_scope,
            "sql": "SELECT public.widgets.id FROM public.widgets",
            "params": [],
        },
        label="foreign workspace database connection",
    )
    assert foreign_database_call["status"] == "failed", foreign_database_call
    assert foreign_database_call["error_code"] == "connection_unavailable"
    assert "workspace" not in json.dumps(foreign_database_call).casefold()
    _assert_no_secrets(
        [
            management_on_database,
            database_on_management,
            foreign_call,
            foreign_database_call,
        ]
    )


async def test_supabase_database_read_is_bounded_and_sql_is_fail_closed(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Database read")
    connection = await _database_connection(
        client,
        workspace_id,
        writer=False,
        max_rows=2,
        statement_timeout_ms=1_000,
    )
    agent = await _create_agent(client, workspace_id, "Database reader")
    connection_id = str(connection["id"])
    scope = {
        "connection_id": connection_id,
        "project_ref": SUPABASE_PROJECT_REF,
        "schema": "public",
    }
    await _grant(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        scope,
    )

    _, cte = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        {
            **scope,
            "sql": (
                "WITH selected AS (SELECT widgets.id FROM public.widgets) "
                "SELECT selected.id FROM selected ORDER BY selected.id"
            ),
            "params": [],
        },
        label="qualified CTE read",
    )
    assert cte["status"] == "completed", cte
    cte_output = cte["sanitized_output_json"]
    assert cte_output["columns"] == ["id"]
    assert cte_output["row_count"] == 2
    assert cte_output["truncated"] is True

    _, text_read = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        {
            **scope,
            "sql": "SELECT widgets.name FROM public.widgets",
            "params": [],
        },
        label="bounded text read",
    )
    assert text_read["status"] == "completed", text_read
    text_output = text_read["sanitized_output_json"]
    assert text_output["row_count"] == 2
    assert len(json.dumps(text_output).encode()) <= 4_096
    assert all(
        cell is None or len(cell.encode()) <= 300 for row in text_output["rows"] for cell in row
    )

    _, wide_text = await _run(
        client,
        workspace_id,
        str(agent["id"]),
        "supabase.database.read",
        {
            **scope,
            "sql": "SELECT widgets.name FROM public.widgets WHERE widgets.id = 3",
            "params": [],
        },
        label="max cell bounded text read",
    )
    assert wide_text["status"] == "completed", wide_text
    wide_output = wide_text["sanitized_output_json"]
    assert wide_output["row_count"] == 1
    assert wide_output["truncated"] is True
    wide_value = wide_output["rows"][0][0]
    assert isinstance(wide_value, str)
    assert wide_value != "x" * 20_000
    assert len(wide_value.encode("utf-8")) <= 256
    _assert_no_secrets([cte, text_read, wide_text])

    before = await _fixture_value(PHASE9_DB_READER_DSN, "SELECT count(*) FROM public.widgets")
    tag = uuid4().hex[:10]
    timeout_table = f"phase9_exit_timeout_{tag}"
    effect_function = f"phase9_exit_effect_{tag}"
    operator_function = f"phase9_exit_operator_{tag}"
    operator_type = f"phase9_exit_value_{tag}"
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f"""
        CREATE TABLE public."{timeout_table}" (id integer NOT NULL);
        INSERT INTO public."{timeout_table}" SELECT generate_series(1, 5000);
        GRANT SELECT ON public."{timeout_table}" TO jhin_reader;

        CREATE TYPE public."{operator_type}" AS (id integer);
        CREATE FUNCTION public."{effect_function}"()
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('phase9-exit-function');
          RETURN 1;
        END
        $function$;
        REVOKE ALL ON FUNCTION public."{effect_function}"() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public."{effect_function}"() TO jhin_reader;

        CREATE FUNCTION private."{operator_function}"(
          left_value public."{operator_type}",
          right_value public."{operator_type}"
        )
        RETURNS boolean
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, private
        AS $function$
        BEGIN
          INSERT INTO side_effects(source) VALUES ('phase9-exit-operator');
          RETURN (left_value).id = (right_value).id;
        END
        $function$;
        REVOKE ALL ON FUNCTION private."{operator_function}"(
          public."{operator_type}", public."{operator_type}"
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION private."{operator_function}"(
          public."{operator_type}", public."{operator_type}"
        ) TO jhin_reader;
        CREATE OPERATOR public.#=# (
          LEFTARG = public."{operator_type}",
          RIGHTARG = public."{operator_type}",
          FUNCTION = private."{operator_function}"
        );
        """,
    )
    try:
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN, "TRUNCATE private.side_effects RESTART IDENTITY"
        )
        assert (
            await _fixture_value(
                PHASE9_DB_READER_DSN,
                f'SELECT public."{effect_function}"()',
            )
            == 1
        )
        assert (
            await _fixture_value(
                PHASE9_DB_READER_DSN,
                f'SELECT CAST(ROW(1) AS public."{operator_type}") '
                f'OPERATOR(public.#=#) CAST(ROW(1) AS public."{operator_type}")',
            )
            is True
        )
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                "SELECT count(*) FROM private.side_effects "
                "WHERE source IN ('phase9-exit-function', 'phase9-exit-operator')",
            )
            == 2
        )
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN, "TRUNCATE private.side_effects RESTART IDENTITY"
        )

        rejected_sql = [
            "SELECT widgets.id FROM widgets",
            "SELECT pg_catalog.current_user",
            "SELECT widgets.id FROM public.widgets; SELECT 2",
            "SELECT widgets.id FROM public.widgets FOR UPDATE",
            "UPDATE public.widgets SET name = 'forbidden' WHERE id = 1",
            f'SELECT public."{effect_function}"()',
            (
                "SELECT widgets.id FROM public.widgets WHERE "
                f'CAST(ROW(widgets.id) AS public."{operator_type}") '
                "OPERATOR(public.#=#) "
                f'CAST(ROW(1) AS public."{operator_type}")'
            ),
            (f'SELECT CAST(ROW(widgets.id) AS public."{operator_type}") FROM public.widgets'),
            "SELECT private.side_effects.id FROM private.side_effects",
        ]
        for index, sql in enumerate(rejected_sql):
            _, call = await _run(
                client,
                workspace_id,
                str(agent["id"]),
                "supabase.database.read",
                {**scope, "sql": sql, "params": []},
                label=f"rejected SQL {index}",
            )
            assert call["status"] == "failed", call
            assert call["error_code"] == "database_sql_not_allowed"
            assert call["sanitized_output_json"]["error"] == "database_sql_not_allowed"
            assert "COUNT(*)" in call["sanitized_output_json"]["hint"]
            assert sql not in json.dumps(call)
        assert (
            await _fixture_value(PHASE9_DB_ADMIN_DSN, "SELECT count(*) FROM private.side_effects")
            == 0
        )

        timeout_connection = await _database_connection(
            client,
            workspace_id,
            writer=False,
            max_rows=1,
            statement_timeout_ms=250,
        )
        timeout_connection_id = str(timeout_connection["id"])
        timeout_scope = {**scope, "connection_id": timeout_connection_id}
        await _grant(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.read",
            timeout_scope,
        )
        started = time.monotonic()
        _, timed_out = await _run(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.read",
            {
                **timeout_scope,
                "sql": (
                    'SELECT a.id AS "statement-timeout-marker" '
                    f'FROM public."{timeout_table}" AS a '
                    f'CROSS JOIN public."{timeout_table}" AS b '
                    "WHERE a.id + b.id < 0"
                ),
                "params": [],
            },
            label="real database statement timeout",
        )
        assert time.monotonic() - started < 5
        assert timed_out["status"] == "failed", timed_out
        assert timed_out["error_code"] == "database_timeout"
        assert timed_out["sanitized_output_json"] == {"error": "database_timeout"}
        assert "statement-timeout-marker" not in json.dumps(timed_out)
    finally:
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f"""
            DROP OPERATOR IF EXISTS public.#=# (
              public."{operator_type}", public."{operator_type}"
            );
            DROP FUNCTION IF EXISTS private."{operator_function}"(
              public."{operator_type}", public."{operator_type}"
            );
            DROP FUNCTION IF EXISTS public."{effect_function}"();
            DROP TYPE IF EXISTS public."{operator_type}";
            DROP TABLE IF EXISTS public."{timeout_table}";
            TRUNCATE private.side_effects RESTART IDENTITY;
            """,
        )
    after = await _fixture_value(PHASE9_DB_READER_DSN, "SELECT count(*) FROM public.widgets")
    assert after == before
    denials = await _audit(client, workspace_id, "tool.call.denied")
    failures = await _audit(client, workspace_id, "tool.call.failed")
    assert denials or failures


async def test_supabase_database_mutations_recheck_live_authority_and_bounds(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    tag = uuid4().hex[:10]
    table = f"phase9_accept_{tag}"
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'CREATE TABLE public."{table}" (id integer PRIMARY KEY, value text NOT NULL)',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'ALTER TABLE public."{table}" ALTER COLUMN value SET STORAGE EXTERNAL',
    )
    await _fixture_execute(
        PHASE9_DB_ADMIN_DSN,
        f'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON public."{table}" TO jhin_writer',
    )
    try:
        workspace_id = await _workspace(client, "Database mutations")
        connection = await _database_connection(client, workspace_id, writer=True, max_rows=5)
        agent = await _create_agent(client, workspace_id, "Database writer")
        connection_id = str(connection["id"])
        scope = {
            "connection_id": connection_id,
            "project_ref": SUPABASE_PROJECT_REF,
            "schema": "public",
        }
        for capability in (
            "supabase.database.read",
            "supabase.database.write",
            "supabase.database.destructive",
        ):
            await _grant(
                client,
                workspace_id,
                str(agent["id"]),
                capability,
                scope,
            )

        missing_scope_agent = await _create_agent(
            client,
            workspace_id,
            "Database writer missing schema scope",
        )
        await _grant(
            client,
            workspace_id,
            str(missing_scope_agent["id"]),
            "supabase.database.write",
            {
                "connection_id": connection_id,
                "project_ref": SUPABASE_PROJECT_REF,
            },
        )
        _, missing_scope_call = await _run(
            client,
            workspace_id,
            str(missing_scope_agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [91, "missing-scope"],
            },
            label="database missing required schema scope",
        )
        assert missing_scope_call["status"] == "denied", missing_scope_call
        assert missing_scope_call["error_code"] == "required_scope_missing"

        wrong_risk = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [90, "wrong-risk"],
            },
            label="database wrong SQL risk tool",
        )
        assert wrong_risk.risk == "destructive"
        _, wrong_risk_call = await _decide(client, workspace_id, wrong_risk)
        assert wrong_risk_call["status"] == "failed", wrong_risk_call
        assert wrong_risk_call["error_code"] == "database_sql_not_allowed"

        disabled_connection = await _database_connection(
            client,
            workspace_id,
            writer=True,
            allow_writes=False,
        )
        disabled_scope = {**scope, "connection_id": str(disabled_connection["id"])}
        await _grant(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            disabled_scope,
        )
        disabled_write = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **disabled_scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [92, "writes-disabled"],
            },
            label="database allow writes false",
        )
        _, disabled_write_call = await _decide(client, workspace_id, disabled_write)
        assert disabled_write_call["status"] == "failed", disabled_write_call
        assert disabled_write_call["error_code"] == "database_writes_disabled"

        reader_connection = await _database_connection(
            client,
            workspace_id,
            writer=False,
            allow_writes=True,
        )
        reader_scope = {**scope, "connection_id": str(reader_connection["id"])}
        await _grant(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            reader_scope,
        )
        reader_write = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **reader_scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [93, "reader-role"],
            },
            label="database read only credential",
        )
        _, reader_write_call = await _decide(client, workspace_id, reader_write)
        assert reader_write_call["status"] == "failed", reader_write_call
        assert reader_write_call["error_code"] == "database_relation_not_allowed"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" WHERE id >= 90',
            )
            == 0
        )

        insert = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [1, "initial"],
            },
            label="bounded insert",
        )
        assert insert.risk == "elevated"
        assert (
            await _fixture_value(PHASE9_DB_ADMIN_DSN, f'SELECT count(*) FROM public."{table}"') == 0
        )
        _, inserted = await _decide(client, workspace_id, insert)
        assert inserted["status"] == "completed", inserted
        assert inserted["sanitized_output_json"] == {"affected_rows": 1}

        update = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'UPDATE public."{table}" SET value = $1 WHERE id = $2',
                "params": ["updated", 1],
            },
            label="bounded update",
        )
        assert update.risk == "destructive"
        _, updated = await _decide(client, workspace_id, update)
        assert updated["status"] == "completed", updated
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN, f'SELECT value FROM public."{table}" WHERE id = 1'
            )
            == "updated"
        )

        delete = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'DELETE FROM public."{table}" WHERE id = $1',
                "params": [1],
            },
            label="bounded delete",
        )
        assert delete.risk == "destructive"
        _, deleted = await _decide(client, workspace_id, delete)
        assert deleted["status"] == "completed", deleted
        assert (
            await _fixture_value(PHASE9_DB_ADMIN_DSN, f'SELECT count(*) FROM public."{table}"') == 0
        )

        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f"INSERT INTO public.\"{table}\" VALUES (1, 'one'), (2, 'two')",
        )
        truncate = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'TRUNCATE TABLE public."{table}" CONTINUE IDENTITY RESTRICT',
                "params": [],
            },
            label="bounded truncate",
        )
        assert truncate.risk == "destructive"
        _, truncated = await _decide(client, workspace_id, truncate)
        assert truncated["status"] == "completed", truncated
        assert truncated["sanitized_output_json"] == {"affected_rows": 2}

        credential_drift = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [4, "credential-drift"],
            },
            label="database credential changed after park",
        )
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
            {"credentials": {"database_url": _internal_database_dsn(writer=False)}},
            expect=200,
        )
        _, credential_drift_call = await _decide(client, workspace_id, credential_drift)
        assert credential_drift_call["status"] == "denied", credential_drift_call
        assert credential_drift_call["error_code"] == "approval_connection_changed"
        await _post(
            client,
            f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
            {"credentials": {"database_url": _internal_database_dsn(writer=True)}},
            expect=200,
        )

        config_drift = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [5, "config-drift"],
            },
            label="database config changed after park",
        )
        await _app_execute(
            "UPDATE connection SET config_json = "
            "jsonb_set(config_json, '{max_rows}', '4'::jsonb) WHERE id::text = $1",
            connection_id,
        )
        _, config_drift_call = await _decide(client, workspace_id, config_drift)
        assert config_drift_call["status"] == "denied", config_drift_call
        assert config_drift_call["error_code"] == "approval_connection_changed"
        await _app_execute(
            "UPDATE connection SET config_json = "
            "jsonb_set(config_json, '{max_rows}', '5'::jsonb) WHERE id::text = $1",
            connection_id,
        )
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" WHERE id IN (4, 5)',
            )
            == 0
        )

        # A live privilege drift after the approval snapshot is rechecked on
        # the same connection before dispatch.
        drifted = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [3, "must-not-land"],
            },
            label="live role drift",
        )
        await _fixture_execute(PHASE9_DB_ADMIN_DSN, "ALTER ROLE jhin_writer BYPASSRLS")
        try:
            _, denied = await _decide(client, workspace_id, drifted)
        finally:
            await _fixture_execute(PHASE9_DB_ADMIN_DSN, "ALTER ROLE jhin_writer NOBYPASSRLS")
        assert denied["status"] == "failed", denied
        assert denied["error_code"] == "database_role_not_least_privilege"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" WHERE id = 3',
            )
            == 0
        )

        inherited_drift = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [6, "inherited-role"],
            },
            label="database inherited dangerous role",
        )
        await _fixture_execute(PHASE9_DB_ADMIN_DSN, "GRANT pg_write_all_data TO jhin_writer")
        try:
            _, inherited_call = await _decide(client, workspace_id, inherited_drift)
        finally:
            await _fixture_execute(
                PHASE9_DB_ADMIN_DSN,
                "REVOKE pg_write_all_data FROM jhin_writer",
            )
        assert inherited_call["status"] == "failed", inherited_call
        assert inherited_call["error_code"] == "database_role_not_least_privilege"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" WHERE id = 6',
            )
            == 0
        )

        trigger_function = f"phase9_exit_trigger_{tag}"
        trigger_name = f"phase9_exit_guard_{tag}"
        trigger_drift = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.write",
            {
                **scope,
                "sql": f'INSERT INTO public."{table}" (id, value) VALUES ($1, $2)',
                "params": [7, "trigger-drift"],
            },
            label="database trigger added after park",
        )
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f"""
            CREATE FUNCTION private."{trigger_function}"()
            RETURNS trigger
            LANGUAGE plpgsql
            VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, private
            AS $function$
            BEGIN
              INSERT INTO side_effects(source) VALUES ('phase9-exit-trigger');
              RETURN NEW;
            END
            $function$;
            CREATE TRIGGER "{trigger_name}"
            BEFORE INSERT ON public."{table}"
            FOR EACH ROW EXECUTE FUNCTION private."{trigger_function}"();
            """,
        )
        try:
            _, trigger_call = await _decide(client, workspace_id, trigger_drift)
        finally:
            await _fixture_execute(
                PHASE9_DB_ADMIN_DSN,
                f"""
                DROP TRIGGER IF EXISTS "{trigger_name}" ON public."{table}";
                DROP FUNCTION IF EXISTS private."{trigger_function}"();
                """,
            )
        assert trigger_call["status"] == "failed", trigger_call
        assert trigger_call["error_code"] == "database_relation_not_allowed"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                "SELECT count(*) FROM private.side_effects WHERE source = 'phase9-exit-trigger'",
            )
            == 0
        )
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" WHERE id = 7',
            )
            == 0
        )

        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f'INSERT INTO public."{table}" VALUES '
            + ", ".join(f"({row}, 'unchanged-{row}')" for row in range(100, 106)),
        )
        over_row_cap = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'UPDATE public."{table}" SET value = $1 WHERE id >= $2',
                "params": ["must-not-update", 100],
            },
            label="database mutation row cap",
        )
        _, over_row_cap_call = await _decide(client, workspace_id, over_row_cap)
        assert over_row_cap_call["status"] == "failed", over_row_cap_call
        assert over_row_cap_call["error_code"] == "database_row_limit_exceeded"
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                f'SELECT count(*) FROM public."{table}" '
                "WHERE id >= 100 AND value LIKE 'unchanged-%'",
            )
            == 6
        )
        await _fixture_execute(
            PHASE9_DB_ADMIN_DSN,
            f'DELETE FROM public."{table}" WHERE id >= 100',
        )

        # DDL never becomes an agent tool, even behind a destructive approval.
        ddl = await _park(
            client,
            workspace_id,
            str(agent["id"]),
            "supabase.database.destructive",
            {
                **scope,
                "sql": f'ALTER TABLE public."{table}" ADD COLUMN forbidden integer',
                "params": [],
            },
            label="DDL unavailable",
        )
        _, ddl_call = await _decide(client, workspace_id, ddl)
        assert ddl_call["status"] == "failed", ddl_call
        assert (
            await _fixture_value(
                PHASE9_DB_ADMIN_DSN,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1 AND column_name = 'forbidden'",
                table,
            )
            == 0
        )
        _assert_no_secrets(
            [
                missing_scope_call,
                wrong_risk_call,
                disabled_write_call,
                reader_write_call,
                inserted,
                updated,
                deleted,
                truncated,
                credential_drift_call,
                config_drift_call,
                denied,
                inherited_call,
                trigger_call,
                over_row_cap_call,
                ddl_call,
            ]
        )
    finally:
        await _fixture_execute(PHASE9_DB_ADMIN_DSN, f'DROP TABLE IF EXISTS public."{table}"')


async def test_control_plane_rbac_access_summary_and_outputs_hide_secrets(
    owner_client: httpx.AsyncClient,
) -> None:
    client = owner_client
    workspace_id = await _workspace(client, "Control plane")
    await _fake_post(FAKE_VERCEL_URL, "/_reset", {})
    connection = await _vercel_connection(client, workspace_id)
    connection_id = str(connection["id"])
    ui_webhook_secret = "control-plane-webhook-secret"
    await _put(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/webhook-secret",
        {"secret": ui_webhook_secret},
        expect=204,
    )
    authorized = await _create_agent(client, workspace_id, "Authorized viewer")
    blocked = await _create_agent(client, workspace_id, "Blocked viewer")
    unrelated = await _create_agent(client, workspace_id, "Unrelated viewer")
    allow_scope = {"connection_id": connection_id, "project_id": "prj_github"}
    allow = await _grant(
        client,
        workspace_id,
        str(authorized["id"]),
        "vercel.project.read",
        allow_scope,
    )
    incomplete = await _grant(
        client,
        workspace_id,
        str(blocked["id"]),
        "vercel.project.read",
        {"connection_id": connection_id},
    )
    deny = await _grant(
        client,
        workspace_id,
        str(blocked["id"]),
        "vercel.project.read",
        allow_scope,
        effect="deny",
    )
    assert {allow["effect"], incomplete["effect"], deny["effect"]} == {"allow", "deny"}

    _, read_call = await _run(
        client,
        workspace_id,
        str(authorized["id"]),
        "vercel.project.read",
        {"connection_id": connection_id, "project_id": "prj_github"},
        label="access summary control",
    )
    assert read_call["status"] == "completed", read_call
    summary = await _get(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/access-summary",
    )
    by_agent = {row["agent_id"]: row for row in summary["agents"]}
    assert set(by_agent) == {str(authorized["id"]), str(blocked["id"])}
    assert str(unrelated["id"]) not in by_agent
    allowed_row = by_agent[str(authorized["id"])]
    blocked_row = by_agent[str(blocked["id"])]
    assert allowed_row["agent_name"] == authorized["name"]
    assert allowed_row["authorized"] is True
    assert allowed_row["authorized_tool_names"] == ["vercel.project.read"]
    assert allowed_row["grants"][0]["scope"] == allow_scope
    assert allowed_row["grants"][0]["eligible_tool_names"] == ["vercel.project.read"]
    assert blocked_row["authorized"] is False
    assert blocked_row["authorized_tool_names"] == []
    assert {row["effect"] for row in blocked_row["grants"]} == {"allow", "deny"}
    assert any(row["eligibility_reason"] for row in blocked_row["grants"])

    viewer_email = f"phase9-viewer-{uuid4().hex[:10]}@example.com"
    viewer_password = f"Phase9-{uuid4().hex}-password"
    viewer_id = uuid4()
    now = datetime.now(UTC)
    app = await asyncpg.connect(_app_database_dsn())
    try:
        await app.execute(
            'INSERT INTO "user" '
            "(id, email, display_name, password_hash, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, 'active', $5, $5)",
            viewer_id,
            viewer_email,
            "Phase 9 Viewer",
            hash_password(viewer_password),
            now,
        )
    finally:
        await app.close()
    await _post(
        client,
        f"/api/v1/workspaces/{workspace_id}/members",
        {"email": viewer_email, "role": "viewer"},
    )

    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as viewer:
        login = await viewer.post(
            "/api/v1/auth/login",
            json={"email": viewer_email, "password": viewer_password},
        )
        assert login.status_code == 200, login.text
        viewer_csrf = _csrf(viewer)
        denied_reads = [
            await viewer.get(f"/api/v1/workspaces/{workspace_id}/connections"),
            await viewer.get(
                f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/access-summary"
            ),
        ]
        assert [response.status_code for response in denied_reads] == [403, 403]
        denied_writes = [
            await viewer.post(
                f"/api/v1/workspaces/{workspace_id}/connections",
                json={
                    "connector_type": "vercel",
                    "name": "Viewer must not create",
                    "auth_type": "access_token",
                    "credentials": {"token": "viewer-secret-must-not-echo"},
                    "config": {"base_url": FAKE_VERCEL_INTERNAL},
                },
                headers=viewer_csrf,
            ),
            await viewer.post(
                f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
                json={"credentials": {"token": "viewer-rotate-must-not-echo"}},
                headers=viewer_csrf,
            ),
            await viewer.put(
                f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/webhook-secret",
                json={"secret": "viewer-webhook-must-not-echo"},
                headers=viewer_csrf,
            ),
        ]
        assert [response.status_code for response in denied_writes] == [403, 403, 403]
        _assert_no_secrets(
            [response.text for response in denied_writes],
            {
                "viewer-secret-must-not-echo",
                "viewer-rotate-must-not-echo",
                "viewer-webhook-must-not-echo",
            },
        )

    # Even an admin session must present the double-submit CSRF token.
    no_csrf = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/rotate",
        json={"credentials": {"token": "csrf-secret-must-not-echo"}},
    )
    assert no_csrf.status_code == 403
    assert "csrf-secret-must-not-echo" not in no_csrf.text

    connection_out = await _get(
        client, f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}"
    )
    recent_calls = await _get(
        client,
        f"/api/v1/workspaces/{workspace_id}/connections/{connection_id}/tool-calls",
    )
    audit_page = await _get(client, f"/api/v1/workspaces/{workspace_id}/audit-events", limit=200)
    _assert_no_secrets(
        [connection_out, summary, recent_calls, audit_page, read_call],
        {ui_webhook_secret},
    )

    ui_response = await client.get(f"{WEB_URL}/connectors", follow_redirects=True)
    assert ui_response.status_code == 200, ui_response.text
    # Connections live on /apps now; /connectors is kept as a permanent
    # redirect for anyone holding the old link (apps/web/next.config.ts).
    assert ui_response.url.path == "/apps", str(ui_response.url)
    # The page is a client component; the initial response proves the protected
    # route renders, while the hydrated connector/access-summary view is
    # exercised by the web component gate and the Task 8 browser verification.
    assert "Loading" in ui_response.text
    _assert_no_secrets(ui_response.text, {ui_webhook_secret})
