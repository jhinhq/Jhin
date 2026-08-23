"""Specific provider failure classes (insufficient_funds) reach the run record."""

from __future__ import annotations

from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from jhin_workflows.agent_task.workflows import _failure_code, _failure_message


def _activity_error(cause: BaseException) -> ActivityError:
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="run_agent_step",
        activity_id="1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    error.__cause__ = cause
    return error


def test_insufficient_funds_type_becomes_the_error_code() -> None:
    message = "Your OpenAI account is out of credit. Add funds at https://x, then retry."
    cause = ApplicationError(message, type="insufficient_funds", non_retryable=True)
    wrapped = _activity_error(cause)
    assert _failure_code(wrapped, "step_failed") == "insufficient_funds"
    assert _failure_message(wrapped) == message


def test_other_types_keep_the_default_code() -> None:
    cause = ApplicationError("openai: HTTP 500", type="model_provider_error")
    assert _failure_code(_activity_error(cause), "step_failed") == "step_failed"
    assert _failure_code(RuntimeError("plain"), "step_failed") == "step_failed"
