"""AvatarGenerationWorkflow orchestration against stub activities: success
returns the asset; exhausted/non-retryable failures record a durable failure
(so the previous avatar stays) instead of failing the workflow."""

from __future__ import annotations

import uuid
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.avatar_generation import (
    ACTIVITY_FAIL_AVATAR_GENERATION,
    ACTIVITY_GENERATE_AVATAR,
    AvatarGenerationInput,
    AvatarGenerationWorkflow,
    FailAvatarGenerationInput,
    GenerateAvatarResult,
    avatar_generation_workflow_id,
)


class Stubs:
    def __init__(self, *, fail_with: ApplicationError | None = None) -> None:
        self.fail_with = fail_with
        self.asset_id = str(uuid.uuid4())
        self.generate_calls = 0
        self.failures: list[FailAvatarGenerationInput] = []

    @activity.defn(name=ACTIVITY_GENERATE_AVATAR)
    async def generate(self, params: AvatarGenerationInput) -> GenerateAvatarResult:
        self.generate_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return GenerateAvatarResult(asset_id=self.asset_id, sha256="a" * 64)

    @activity.defn(name=ACTIVITY_FAIL_AVATAR_GENERATION)
    async def fail(self, params: FailAvatarGenerationInput) -> None:
        self.failures.append(params)


async def run_workflow(stubs: Stubs) -> Any:
    params = AvatarGenerationInput(
        workspace_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        generation_id=str(uuid.uuid4()),
    )
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        task_queue = f"test-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AvatarGenerationWorkflow],
            activities=[stubs.generate, stubs.fail],
        ):
            return await env.client.execute_workflow(
                AvatarGenerationWorkflow.run,
                params,
                id=avatar_generation_workflow_id(params.generation_id),
                task_queue=task_queue,
            )
    finally:
        await env.shutdown()


async def test_success_returns_the_activated_asset() -> None:
    stubs = Stubs()
    result = await run_workflow(stubs)
    assert result.status == "succeeded"
    assert result.asset_id == stubs.asset_id
    assert stubs.failures == []


async def test_non_retryable_failure_is_recorded_once() -> None:
    stubs = Stubs(
        fail_with=ApplicationError(
            "profile cannot generate images",
            type="image_generation_unsupported",
            non_retryable=True,
        )
    )
    result = await run_workflow(stubs)
    assert result.status == "failed"
    assert result.error_code == "image_generation_unsupported"
    assert stubs.generate_calls == 1
    assert len(stubs.failures) == 1
    assert stubs.failures[0].error_code == "image_generation_unsupported"
    assert stubs.failures[0].error == "profile cannot generate images"


async def test_retryable_failure_exhausts_retries_then_records_failure() -> None:
    stubs = Stubs(fail_with=ApplicationError("provider 503", type="provider_error"))
    result = await run_workflow(stubs)
    assert result.status == "failed"
    assert stubs.generate_calls == 3
    assert len(stubs.failures) == 1
    assert stubs.failures[0].error_code == "provider_error"
