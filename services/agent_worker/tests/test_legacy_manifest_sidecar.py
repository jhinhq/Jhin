"""Frozen Phase 9 manifests gain only an exact private reasoning sidecar."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError
from test_reasoning_manifest import ReasoningWorld
from test_reasoning_manifest import world as _base_world_fixture

from jhin_db.models import RunEvent, ToolCall
from jhin_models import ModelResponse, ModelToolCall, ModelUsage


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


def model_call(
    provider_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> ModelResponse:
    import json

    return ModelResponse(
        text="repair",
        finish_reason="tool_calls",
        model="legacy-repair",
        usage=ModelUsage(input_tokens=4, output_tokens=2),
        latency_ms=3,
        provider_request_id="repair-request",
        tool_calls=(
            ModelToolCall(
                id=provider_call_id,
                name=tool_name,
                arguments_json=json.dumps(arguments, separators=(",", ":"), sort_keys=True),
            ),
        ),
    )


async def _seed_manifest(
    world: ReasoningWorld,
    *,
    value: str,
) -> None:
    async with world.sessions() as session:
        session.add(
            RunEvent(
                workspace_id=world.workspace_id,
                task_id=world.task_id,
                run_id=world.run_id,
                seq=0,
                event_type="agent.step.tool_manifest",
                payload_json={
                    "step": 0,
                    "manifest": {
                        "count": 1,
                        "calls": [
                            {
                                "ordinal": 0,
                                "lossless": True,
                                "tool_name": "system.echo",
                                "arguments_json": f'{{"value":"{value}"}}',
                            }
                        ],
                    },
                },
            )
        )
        await session.commit()


async def test_phase9_manifest_without_reasoning_is_rebound_before_any_effect(
    reasoning_world: ReasoningWorld,
) -> None:
    await _seed_manifest(reasoning_world, value="same")
    reasoning_world.model.responses.append(
        model_call("replacement-provider-id", "system.echo", {"value": "same"})
    )

    result = await reasoning_world.reasoning.reason_agent_step(
        reasoning_world.params,
        legacy_sidecar_repair=True,
    )

    manifest = await reasoning_world.load_event("agent.step.tool_manifest")
    reasoning = await reasoning_world.load_event("agent.step.reasoning")
    assert result.call_count == 1
    assert set(manifest.payload_json) == {"step", "manifest"}
    assert reasoning.payload_json["provider_call_ids"] == ["replacement-provider-id"]
    assert await reasoning_world.count_events("agent.step.reasoning") == 1
    assert reasoning_world.effect.count == 0


async def test_phase9_sidecar_repair_rejects_canonical_drift_before_effect(
    reasoning_world: ReasoningWorld,
) -> None:
    await _seed_manifest(reasoning_world, value="bound")
    reasoning_world.model.responses.append(
        model_call("retry", "system.echo", {"value": "changed"})
    )

    with pytest.raises(ApplicationError) as error:
        await reasoning_world.reasoning.reason_agent_step(
            reasoning_world.params,
            legacy_sidecar_repair=True,
        )

    assert error.value.type == "tool_step_manifest_drift"
    assert error.value.non_retryable is False
    assert reasoning_world.effect.count == 0
    assert await reasoning_world.count_events("agent.step.reasoning") == 0
    async with reasoning_world.sessions() as session:
        assert await session.scalar(select(func.count(ToolCall.id))) == 0
