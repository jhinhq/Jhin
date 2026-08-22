"""AvatarGenerationWorkflow: one durable stylized-avatar render.

Started by the API after committing a queued ``avatar_generation`` row, under
the deterministic id ``avatar-generation-<id>`` so a retried request cannot
render twice. The generate activity does everything transactional (provider
call → safe normalization → atomic activation); when it ultimately fails the
workflow records the failure durably so the UI sees a terminal status and the
agent's previous avatar stays active.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from jhin_workflows.avatar_generation.shared import (
    ACTIVITY_FAIL_AVATAR_GENERATION,
    ACTIVITY_GENERATE_AVATAR,
    AvatarGenerationInput,
    AvatarGenerationResult,
    FailAvatarGenerationInput,
    GenerateAvatarResult,
)

# Provider 5xx/network blips retry briefly; semantic dead ends (no capable
# profile, rejected image) raise non-retryable ApplicationErrors.
_GENERATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)
_FAIL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=5,
)


def _error_details(exc: Exception) -> tuple[str, str]:
    cause = exc.cause if isinstance(exc, ActivityError) else exc
    if isinstance(cause, ApplicationError):
        return (cause.type or "generation_failed"), (cause.message or "generation failed")
    return "generation_failed", (str(cause) or "generation failed")


@workflow.defn(name="AvatarGenerationWorkflow")
class AvatarGenerationWorkflow:
    @workflow.run
    async def run(self, params: AvatarGenerationInput) -> AvatarGenerationResult:
        try:
            generated: GenerateAvatarResult = await workflow.execute_activity(
                ACTIVITY_GENERATE_AVATAR,
                params,
                result_type=GenerateAvatarResult,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=_GENERATE_RETRY,
            )
        except ActivityError as exc:
            code, message = _error_details(exc)
            await workflow.execute_activity(
                ACTIVITY_FAIL_AVATAR_GENERATION,
                FailAvatarGenerationInput(
                    workspace_id=params.workspace_id,
                    agent_id=params.agent_id,
                    generation_id=params.generation_id,
                    error_code=code,
                    error=message[:2_000],
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_FAIL_RETRY,
            )
            return AvatarGenerationResult(
                generation_id=params.generation_id, status="failed", error_code=code
            )
        return AvatarGenerationResult(
            generation_id=params.generation_id,
            status="succeeded",
            asset_id=generated.asset_id,
        )
