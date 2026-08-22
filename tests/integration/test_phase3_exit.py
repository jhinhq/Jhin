"""Phase 3 exit test (plan 45): two agents with different model profiles each
run a Temporal-backed task end-to-end against the running compose stack.

Flow: login (seeding on demand) → encrypted secret → provider pointing at the
in-stack fake OpenAI-compatible provider → two priced profiles → two agents →
assign a task to each → both AgentTaskWorkflows complete on the agent worker →
runs persist distinct model_profile_ids with usage/cost, timeline events
exist, and the conversational message endpoint round-trips.

Everything goes over HTTP to the containerized API so the same database the
agent worker uses is observed — this is the deployed path, not a simulation.
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

# The agent worker resolves this hostname on the compose network.
FAKE_PROVIDER_URL = "http://fake-provider:8080/v1"
TASK_TIMEOUT_SECONDS = 90.0


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


async def _wait_for_task(
    client: httpx.AsyncClient, workspace_id: str, task_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task_id} did not finish in {TASK_TIMEOUT_SECONDS}s: {detail}")


async def test_two_agents_two_profiles_run_through_temporal(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]  # unique names: the test may run repeatedly on one dev DB

    # 1. Credential in the encrypted store (decrypted only inside the worker).
    secret = await _post(
        client,
        f"/api/v1/workspaces/{ws}/secrets",
        {"name": f"exit-test-key-{tag}", "value": f"sk-exit-test-{tag}", "type": "api_key"},
    )
    assert "value" not in secret and secret["masked_hint"].endswith(tag[-4:])

    # 2. Provider on the fake endpoint, with a live verify through the adapter.
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"Exit Test Provider {tag}",
            "base_url": FAKE_PROVIDER_URL,
            "secret_id": secret["id"],
        },
    )
    verify = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers/{provider['id']}/verify",
        {},
        expect=200,
    )
    assert verify["ok"], verify

    # 3. Two profiles with different models and different pricing.
    pricing = (("fake-mini", 150_000, 600_000), ("fake-pro", 2_500_000, 10_000_000))
    profiles: dict[str, dict[str, Any]] = {}
    for model, input_cost, output_cost in pricing:
        profiles[model] = await _post(
            client,
            f"/api/v1/workspaces/{ws}/model-profiles",
            {
                "provider_id": provider["id"],
                "model_name": model,
                "display_name": f"Exit {model} {tag}",
                "input_cost_micros_per_million": input_cost,
                "output_cost_micros_per_million": output_cost,
            },
        )

    # 4. Two agents, each pinned to a different profile.
    agents: dict[str, dict[str, Any]] = {}
    for model in ("fake-mini", "fake-pro"):
        agents[model] = await _post(
            client,
            f"/api/v1/workspaces/{ws}/agents",
            {
                "name": f"Exit Agent {model} {tag}",
                "role_title": "Integration Tester",
                "system_prompt": "You complete integration test tasks precisely.",
                "model_profile_id": profiles[model]["id"],
            },
        )

    # 5. Assign one task to each agent; both run through Temporal.
    tasks: dict[str, dict[str, Any]] = {}
    for model, agent in agents.items():
        tasks[model] = await _post(
            client,
            f"/api/v1/workspaces/{ws}/agents/{agent['id']}/assign-task",
            {
                "title": f"Exit task for {model} {tag}",
                "description": f"Summarize the Phase 3 exit criteria ({model}).",
            },
        )
        assert tasks[model]["temporal_workflow_id"] == f"task-{tasks[model]['id']}"

    for model, task in tasks.items():
        detail = await _wait_for_task(client, ws, task["id"])
        assert detail["task"]["state"] == "completed", detail

        # Exactly one run, on the right profile, with usage and cost recorded.
        runs = detail["runs"]
        assert len(runs) == 1
        run = runs[0]
        assert run["status"] == "completed"
        assert run["model_profile_id"] == profiles[model]["id"]
        assert run["input_tokens"] > 0 and run["output_tokens"] > 0
        assert run["estimated_cost_micros"] > 0
        assert run["steps_used"] == 1
        assert len(run["snapshot_hash"]) == 64
        assert detail["total_cost_micros"] == run["estimated_cost_micros"]

        # Timeline includes the durable manifest/reasoning pair and committed-step markers.
        response = await client.get(f"/api/v1/workspaces/{ws}/tasks/{task['id']}/timeline")
        timeline = response.json()
        events = [e["event_type"] for e in timeline]
        assert events == [
            "run.started",
            # Memory retrieval provenance is recorded inside the locked bind
            # transaction, immediately before the manifest/reasoning pair.
            "memory.retrieved",
            "agent.step.tool_manifest",
            "agent.step.reasoning",
            "node.load_context",
            "node.reason",
            "agent.step.committed",
            "run.completed",
        ], events
        reasoning = [e for e in timeline if e["event_type"] == "agent.step.reasoning"]
        assert len(reasoning) == 1
        assert reasoning[0]["payload_json"] == {}

        # The agent's reply proves which model served it (fake echoes [model]).
        response = await client.get(f"/api/v1/workspaces/{ws}/tasks/{task['id']}/messages")
        agent_messages = [m for m in response.json() if m["sender_type"] == "agent"]
        assert len(agent_messages) == 1
        assert agent_messages[0]["content_json"]["text"].startswith(f"[{model}]")

    # The pricier model must cost more for comparable work.
    async def total_cost(model: str) -> int:
        response = await client.get(f"/api/v1/workspaces/{ws}/tasks/{tasks[model]['id']}")
        cost: int = response.json()["total_cost_micros"]
        return cost

    assert await total_cost("fake-pro") > await total_cost("fake-mini")


async def test_message_agent_creates_conversational_task(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]

    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"Chat Provider {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": "fake-mini",
            "display_name": f"Chat profile {tag}",
            "input_cost_micros_per_million": 100_000,
            "output_cost_micros_per_million": 400_000,
        },
    )
    agent = await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"Chat Agent {tag}",
            "system_prompt": "You answer user messages helpfully.",
            "model_profile_id": profile["id"],
        },
    )

    greeting = f"Hello agent, please acknowledge run {tag}."
    task = await _post(
        client, f"/api/v1/workspaces/{ws}/agents/{agent['id']}/message", {"text": greeting}
    )
    detail = await _wait_for_task(client, ws, task["id"])
    assert detail["task"]["state"] == "completed", detail

    response = await client.get(f"/api/v1/workspaces/{ws}/tasks/{task['id']}/messages")
    messages = response.json()
    senders = [m["sender_type"] for m in messages]
    assert senders == ["user", "agent"], senders
    assert messages[0]["content_json"]["text"] == greeting
    assert tag in messages[1]["content_json"]["text"]  # fake echoes the instruction
