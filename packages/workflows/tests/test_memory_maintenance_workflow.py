"""MemoryMaintenanceWorkflow orchestration with stub activities: typed
failure results (never raised), idempotent ids, and the best-effort starter."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.memory_maintenance import (
    ACTIVITY_APPLY_MEMORY_CANDIDATES,
    ACTIVITY_EXTRACT_MEMORY_CANDIDATES,
    ApplyMemoryCandidatesInput,
    ApplyMemoryCandidatesResult,
    ExtractMemoryCandidatesInput,
    ExtractMemoryCandidatesResult,
    MemoryMaintenanceInput,
    MemoryMaintenanceResult,
    MemoryMaintenanceWorkflow,
    memory_maintenance_workflow_id,
    start_memory_maintenance,
)


class Stubs:
    def __init__(
        self,
        *,
        extraction: ExtractMemoryCandidatesResult | None = None,
        apply: ApplyMemoryCandidatesResult | None = None,
        extract_raises: bool = False,
        apply_raises: bool = False,
    ) -> None:
        self.extraction = extraction or ExtractMemoryCandidatesResult(
            ok=True, candidates_json=[{"content": "Ava prefers concise updates."}]
        )
        self.apply_result = apply or ApplyMemoryCandidatesResult(
            ok=True, created_ids=["m1"], activated=1
        )
        self.extract_raises = extract_raises
        self.apply_raises = apply_raises
        self.extract_calls: list[ExtractMemoryCandidatesInput] = []
        self.apply_calls: list[ApplyMemoryCandidatesInput] = []

    @activity.defn(name=ACTIVITY_EXTRACT_MEMORY_CANDIDATES)
    async def extract(self, params: ExtractMemoryCandidatesInput) -> ExtractMemoryCandidatesResult:
        self.extract_calls.append(params)
        if self.extract_raises:
            raise ApplicationError("provider down", type="provider", non_retryable=True)
        return self.extraction

    @activity.defn(name=ACTIVITY_APPLY_MEMORY_CANDIDATES)
    async def apply(self, params: ApplyMemoryCandidatesInput) -> ApplyMemoryCandidatesResult:
        self.apply_calls.append(params)
        if self.apply_raises:
            raise ApplicationError("db down", type="db", non_retryable=True)
        return self.apply_result


def make_input(**overrides: Any) -> MemoryMaintenanceInput:
    values: dict[str, Any] = {
        "workspace_id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "source_kind": "message",
        "source_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
    }
    values.update(overrides)
    return MemoryMaintenanceInput(**values)


async def run_workflow(stubs: Stubs, params: MemoryMaintenanceInput) -> MemoryMaintenanceResult:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MemoryMaintenanceWorkflow],
            activities=[stubs.extract, stubs.apply],
        ):
            status, handle = await start_memory_maintenance(
                env.client, params, task_queue=task_queue
            )
            assert status == "started"
            assert handle is not None
            result: MemoryMaintenanceResult = await handle.result()
            return result
    finally:
        await env.shutdown()


def test_workflow_id_is_deterministic() -> None:
    params = make_input(source_id="abc", turn_marker="")
    assert memory_maintenance_workflow_id(params) == "memory-maintenance-message-abc"
    params.turn_marker = "run-1"
    assert memory_maintenance_workflow_id(params) == "memory-maintenance-message-abc-run-1"


async def test_happy_path_passes_user_intent_verbatim() -> None:
    stubs = Stubs()
    params = make_input(
        remember_enabled=True,
        requested_scope="team",
        actor_user_id="user-1",
        actor_authority="workspace",
    )
    result = await run_workflow(stubs, params)
    assert result.status == "applied"
    assert result.candidate_count == 1
    assert result.apply is not None and result.apply.activated == 1
    assert len(stubs.extract_calls) == 1
    applied = stubs.apply_calls[0]
    assert applied.remember_enabled is True
    assert applied.requested_scope == "team"
    assert applied.actor_user_id == "user-1"
    assert applied.actor_authority == "workspace"
    assert applied.idempotency_key == memory_maintenance_workflow_id(params)


async def test_no_candidates_skips_apply() -> None:
    stubs = Stubs(extraction=ExtractMemoryCandidatesResult(ok=True, candidates_json=[]))
    result = await run_workflow(stubs, make_input())
    assert result.status == "nothing_to_remember"
    assert stubs.apply_calls == []


async def test_malformed_extraction_is_a_typed_result() -> None:
    stubs = Stubs(extraction=ExtractMemoryCandidatesResult(ok=False, error="malformed_output"))
    result = await run_workflow(stubs, make_input())
    assert result.status == "extraction_failed"
    assert result.extraction_error == "malformed_output"
    assert stubs.apply_calls == []


async def test_extraction_activity_failure_never_fails_the_workflow() -> None:
    result = await run_workflow(Stubs(extract_raises=True), make_input())
    assert result.status == "extraction_failed"
    assert result.extraction_error == "ActivityError"


async def test_apply_activity_failure_never_fails_the_workflow() -> None:
    result = await run_workflow(Stubs(apply_raises=True), make_input())
    assert result.status == "apply_failed"


async def test_starter_is_idempotent_and_validates_input() -> None:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        stubs = Stubs()
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MemoryMaintenanceWorkflow],
            activities=[stubs.extract, stubs.apply],
        ):
            params = make_input()
            first, handle = await start_memory_maintenance(
                env.client, params, task_queue=task_queue
            )
            assert first == "started" and handle is not None
            second, dup = await start_memory_maintenance(env.client, params, task_queue=task_queue)
            assert second == "duplicate" and dup is None
            await handle.result()
            assert len(stubs.extract_calls) == 1
            invalid, _ = await start_memory_maintenance(
                env.client, make_input(source_kind="transcript"), task_queue=task_queue
            )
            assert invalid == "invalid"
    finally:
        await env.shutdown()


async def test_starter_reports_failure_instead_of_raising() -> None:
    class BrokenClient:
        async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("temporal unreachable")

    status, handle = await start_memory_maintenance(BrokenClient(), make_input())  # type: ignore[arg-type]
    assert status == "failed"
    assert handle is None
