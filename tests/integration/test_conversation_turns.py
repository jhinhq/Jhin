"""Chat turns against the running compose stack: /conversations end to end.

(a) THREE TURNS — a conversation, then two follow-ups, each sent only after
    the previous task completed so all three take the new_task path. Every
    reply must answer the question that was *just* asked. The fake provider
    echoes the last user message it saw ("[{model}] Completed: {text}"), so
    the reply text is a direct read-out of prompt ordering: if the current
    question is not the newest user turn reaching the provider, the echo
    names the previous one and this test says so. That is the live guard for
    the regression where turn 2 answered turn 1 and turn 3 repeated turn 2.
(b) MID-RUN TURN — a turn sent while the first run is still live is delivered
    into that run as an instruction, and no second task is forked.

The mid-run window is opened with an approval-gated tool rather than
``FAKE_PROVIDER_LATENCY_MS``: the parked run holds the task in an active
state for as long as the test needs, whatever the provider's latency happens
to be on this stack, so there is no race to lose and nothing to skip when the
variable is left at its default. Raising the variable by hand is still the way
to exercise the same controls interactively.
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
MODEL_NAME = "fake-mini"
TASK_TIMEOUT_SECONDS = 120.0
PARK_TIMEOUT_SECONDS = 90.0


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


async def _make_agent(client: httpx.AsyncClient, ws: str, tag: str, name: str) -> dict[str, Any]:
    """Provider + profile + agent wired to the in-stack fake provider."""
    provider = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-providers",
        {
            "type": "openai_compatible",
            "display_name": f"Chat provider {name} {tag}",
            "base_url": FAKE_PROVIDER_URL,
        },
    )
    profile = await _post(
        client,
        f"/api/v1/workspaces/{ws}/model-profiles",
        {
            "provider_id": provider["id"],
            "model_name": MODEL_NAME,
            "display_name": f"Chat profile {name} {tag}",
            "input_cost_micros_per_million": 100_000,
            "output_cost_micros_per_million": 400_000,
        },
    )
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents",
        {
            "name": f"Chat {name} {tag}",
            "system_prompt": "You answer the person's latest question.",
            "model_profile_id": profile["id"],
        },
    )


async def _grant(client: httpx.AsyncClient, ws: str, agent_id: str, capability: str) -> None:
    await _post(
        client,
        f"/api/v1/workspaces/{ws}/agents/{agent_id}/grants",
        {"capability": capability, "scope": {}, "effect": "allow"},
    )


async def _open_chat(
    client: httpx.AsyncClient, ws: str, agent_id: str, text: str
) -> tuple[str, str]:
    """Start a conversation with its first turn; return (conversation, task)."""
    detail = await _post(
        client, f"/api/v1/workspaces/{ws}/conversations", {"agent_id": agent_id, "text": text}
    )
    assert len(detail["tasks"]) == 1, detail
    return detail["conversation"]["id"], detail["tasks"][0]["id"]


async def _turn(
    client: httpx.AsyncClient, ws: str, conversation_id: str, text: str
) -> dict[str, Any]:
    return await _post(
        client,
        f"/api/v1/workspaces/{ws}/conversations/{conversation_id}/turns",
        {"text": text},
        expect=200,
    )


async def _wait_for_task(
    client: httpx.AsyncClient, ws: str, task_id: str, budget: float = TASK_TIMEOUT_SECONDS
) -> dict[str, Any]:
    deadline = time.monotonic() + budget
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}")
        if detail["task"]["state"] in ("completed", "failed", "cancelled"):
            return detail
        await asyncio.sleep(0.5)
    pytest.fail(f"task {task_id} did not finish in {budget}s: {detail}")


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


async def _agent_reply(client: httpx.AsyncClient, ws: str, task_id: str) -> str:
    messages = await _get(client, f"/api/v1/workspaces/{ws}/tasks/{task_id}/messages")
    replies = [m for m in messages if m["sender_type"] == "agent"]
    assert len(replies) == 1, replies
    text: str = replies[0]["content_json"]["text"]
    return text


async def _thread(client: httpx.AsyncClient, ws: str, conversation_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = await _get(
        client, f"/api/v1/workspaces/{ws}/conversations/{conversation_id}/messages"
    )
    return messages


# --- (a) three sequential turns ----------------------------------------------


async def test_three_turn_chat_answers_the_question_just_asked(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag, "three-turn")

    questions = [
        f"What is our deployment cadence? ({tag} one)",
        f"And who approves a release? ({tag} two)",
        f"Name the rollback step. ({tag} three)",
    ]

    conversation_id, first_task = await _open_chat(client, ws, agent["id"], questions[0])
    task_ids = [first_task]
    detail = await _wait_for_task(client, ws, first_task)
    assert detail["task"]["state"] == "completed", detail

    for question in questions[1:]:
        turn = await _turn(client, ws, conversation_id, question)
        # Nothing is running, so each follow-up opens its own work episode --
        # the path where the prompt has to carry earlier turns *and* the new
        # question, which is where the ordering bug lived.
        assert turn["mode"] == "new_task", turn
        task_ids.append(turn["task_id"])
        detail = await _wait_for_task(client, ws, turn["task_id"])
        assert detail["task"]["state"] == "completed", detail

    assert len(set(task_ids)) == 3

    # The oracle: the fake provider echoes the last user message it was sent.
    # An echo naming an earlier question is the exact reported symptom.
    for question, task_id in zip(questions, task_ids, strict=True):
        assert await _agent_reply(client, ws, task_id) == f"[{MODEL_NAME}] Completed: {question}"

    # And the thread a person reads back: strictly alternating, every answer
    # against the question above it.
    thread = await _thread(client, ws, conversation_id)
    assert [m["sender_type"] for m in thread] == ["user", "agent"] * 3
    assert [m["content_json"]["text"] for m in thread] == [
        text
        for question in questions
        for text in (question, f"[{MODEL_NAME}] Completed: {question}")
    ]


# --- (b) a turn sent while the run is still live ------------------------------


async def test_turn_sent_mid_run_is_delivered_as_an_instruction(
    owner: tuple[httpx.AsyncClient, str],
) -> None:
    client, ws = owner
    tag = uuid4().hex[:8]
    agent = await _make_agent(client, ws, tag, "mid-run")
    await _grant(client, ws, agent["id"], "system.demo.destructive")
    await _grant(client, ws, agent["id"], "system.echo")

    # The approval gate is what holds the run open: the workflow parks on the
    # human decision and stays parked until this test approves.
    park = f'[[tool:system.demo.destructive {{"label": "midrun-{tag}"}}]]'
    conversation_id, task_id = await _open_chat(client, ws, agent["id"], f"Please run it: {park}")
    run = await _wait_for_parked_run(client, ws, task_id)

    # Mid-run: the person adds something while the first run is still live.
    # The marker rides along so the delivery is provable, not just recorded.
    follow_up = f'Also echo this: [[tool:system.echo {{"text": "midrun-{tag}"}}]]'
    turn = await _turn(client, ws, conversation_id, follow_up)
    assert turn["mode"] == "instruction"
    assert turn["task_id"] == task_id
    assert turn["message"]["message_type"] == "instruction"

    detail = await _get(client, f"/api/v1/workspaces/{ws}/conversations/{conversation_id}")
    assert [t["id"] for t in detail["tasks"]] == [task_id]
    assert detail["conversation"]["task_count"] == 1
    assert detail["conversation"]["active_task_id"] == task_id

    approvals = await _get(
        client, f"/api/v1/workspaces/{ws}/approvals", status="pending", limit=200
    )
    pending = [a for a in approvals["items"] if a["task_id"] == task_id]
    assert len(pending) == 1, approvals
    await _post(
        client, f"/api/v1/workspaces/{ws}/approvals/{pending[0]['id']}/approve", {}, expect=200
    )

    finished = await _wait_for_task(client, ws, task_id)
    assert finished["task"]["state"] == "completed", finished
    assert len(finished["runs"]) == 1 and finished["runs"][0]["id"] == run["id"]

    # The instruction reached the model, and not merely the database: the echo
    # call can only come from words that were in the prompt, and only the
    # follow-up carried them. The count is deliberately not pinned — restating
    # a drained instruction is the accepted safe direction when the history
    # row and the live signal ever render differently.
    calls = await _get(client, f"/api/v1/workspaces/{ws}/runs/{run['id']}/tool-calls")
    assert calls and calls[0]["tool_name"] == "system.demo.destructive"
    echoes = [c for c in calls if c["tool_name"] == "system.echo"]
    assert echoes, calls
    assert all(c["sanitized_input_json"] == {"text": f"midrun-{tag}"} for c in echoes)
    assert all(c["status"] == "completed" for c in echoes)

    # Still one task, and the follow-up is in the thread the person reads.
    thread = await _thread(client, ws, conversation_id)
    assert [m["message_type"] for m in thread[:2]] == ["text", "instruction"]
    assert thread[1]["content_json"]["text"] == follow_up
