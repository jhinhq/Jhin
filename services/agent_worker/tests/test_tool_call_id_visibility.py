"""An agent can read back the Jhin id of a call it made.

Observed live: ``ghost.assignment.attach_evidence`` takes Jhin ``ToolCall``
row UUIDs, and nothing in the observation the writer saw carried one. The only
id anywhere in its transcript was the provider's own call id
("call_3rq2qq9d"), which is not what the tool wants, so the agent guessed
UUIDs, the gateway rejected them with ``ghost_evidence_invalid``, and the run
stalled until a person pasted the real ids in.

These tests run the real reasoning activity against real rows and assert on
what the model client was handed, then push the same messages through the
context budget: a long research result is exactly when an id buried in the
body would be excerpted away, and exactly when the evidence matters.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
from sqlalchemy import select
from test_reasoning_manifest import ReasoningWorld
from test_reasoning_manifest import world as _base_world_fixture

from jhin_agents.context import TOOL_CALL_ID_LABEL, UNTRUSTED_LABEL
from jhin_agents.context_budget import budget_context
from jhin_db.models import Message, Task, ToolCall
from jhin_domain import MessageVisibility, RecipientType, SenderType
from jhin_models import ModelMessage, ModelResponse, ModelUsage

ARCHIVE_STATUS = "ghost.archive.status"
PROVIDER_CALL_ID = "call_3rq2qq9d"


@pytest.fixture(name="reasoning_world")
async def _reasoning_world_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ReasoningWorld]:
    fixture_impl = cast(
        Callable[[pytest.MonkeyPatch], AsyncIterator[ReasoningWorld]],
        vars(_base_world_fixture)["__wrapped__"],
    )
    async for value in fixture_impl(monkeypatch):
        yield value


def plain_reply() -> ModelResponse:
    return ModelResponse(
        text="Noted.",
        finish_reason="stop",
        model="reasoning-test",
        usage=ModelUsage(input_tokens=5, output_tokens=2, cached_tokens=0),
        latency_ms=2,
        provider_request_id="provider-request-evidence",
        tool_calls=(),
    )


async def seed_completed_call(world: ReasoningWorld, *, output: dict[str, object]) -> ToolCall:
    """One completed retrieval, projected into the transcript the way
    ``AgentToolProjectionActivities`` writes it after the gateway ran it."""
    async with world.sessions() as session:
        task = await session.get(Task, world.task_id)
        assert task is not None
        agent_id = task.assigned_agent_id
        row = ToolCall(
            workspace_id=world.workspace_id,
            run_id=world.run_id,
            agent_id=agent_id,
            tool_name=ARCHIVE_STATUS,
            sanitized_input_json={},
            sanitized_output_json=output,
            status="completed",
        )
        session.add(row)
        await session.flush()
        for message_type, content in (
            (
                "tool_call",
                {
                    "text": "Checking the archive.",
                    "tool_call_id": str(row.id),
                    "provider_call_id": PROVIDER_CALL_ID,
                    "tool_name": ARCHIVE_STATUS,
                    "arguments_json": "{}",
                },
            ),
            (
                "tool_result",
                {
                    "tool_call_id": str(row.id),
                    "provider_call_id": PROVIDER_CALL_ID,
                    "tool_name": ARCHIVE_STATUS,
                    "status": "executed",
                    "result": json.dumps(output, ensure_ascii=False),
                },
            ),
        ):
            session.add(
                Message(
                    workspace_id=world.workspace_id,
                    task_id=world.task_id,
                    run_id=world.run_id,
                    sender_type=SenderType.AGENT.value,
                    sender_id=agent_id,
                    recipient_type=RecipientType.TASK.value,
                    recipient_id=world.task_id,
                    message_type=message_type,
                    content_json=content,
                    visibility=MessageVisibility.INTERNAL.value,
                )
            )
        await session.commit()
        return row


def tool_message(world: ReasoningWorld) -> ModelMessage:
    return next(message for message in world.model.requests[0].messages if message.role == "tool")


async def test_the_result_carries_the_jhin_tool_call_id(reasoning_world: ReasoningWorld) -> None:
    row = await seed_completed_call(reasoning_world, output={"posts": 4498, "synced": True})
    reasoning_world.model.responses.append(plain_reply())

    await reasoning_world.reasoning.reason_agent_step_activity(reasoning_world.params)

    observation = tool_message(reasoning_world)
    assert f"{TOOL_CALL_ID_LABEL}{row.id}" in observation.content
    # The id the model can already see is the provider's, and it is not the
    # one attach_evidence wants; the two must not be confusable.
    assert PROVIDER_CALL_ID not in observation.content
    async with reasoning_world.sessions() as session:
        persisted = list(await session.scalars(select(ToolCall.id)))
    assert [str(identifier) for identifier in persisted] == [str(row.id)]


async def test_the_id_survives_a_shortened_research_result(
    reasoning_world: ReasoningWorld,
) -> None:
    """A long archive search is the case that stalled the live run, and the
    budget replaces its body with a head/tail excerpt."""
    body = "p" * 60_000
    row = await seed_completed_call(reasoning_world, output={"body": body})
    reasoning_world.model.responses.append(plain_reply())

    await reasoning_world.reasoning.reason_agent_step_activity(reasoning_world.params)
    budgeted = budget_context(
        reasoning_world.model.requests[0].messages,
        (),
        provider_type="ollama",
        context_window=16_384,
        max_output_tokens=1024,
    )

    assert budgeted.shortened_messages >= 1
    observation = next(message for message in budgeted.messages if message.role == "tool")
    assert body not in observation.content
    assert observation.content.startswith(UNTRUSTED_LABEL)
    assert f"{TOOL_CALL_ID_LABEL}{row.id}" in observation.content


async def test_the_id_survives_the_smallest_excerpt(reasoning_world: ReasoningWorld) -> None:
    """The last excerpt size the budget tries keeps 96 bytes of the body, so
    an id anywhere but the front of it would be gone by then."""
    row = await seed_completed_call(reasoning_world, output={"body": "p" * 60_000})
    reasoning_world.model.responses.append(plain_reply())

    await reasoning_world.reasoning.reason_agent_step_activity(reasoning_world.params)
    observation = tool_message(reasoning_world)
    budgeted = budget_context(
        (ModelMessage(role="system", content="S"), observation),
        (),
        provider_type="ollama",
        context_window=1_536,
        max_output_tokens=64,
    )

    shortest = next(message for message in budgeted.messages if message.role == "tool")
    assert len(shortest.content) < 512
    assert f"{TOOL_CALL_ID_LABEL}{row.id}" in shortest.content
