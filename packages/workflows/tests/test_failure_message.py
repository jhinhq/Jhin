"""Run failure text comes from the most specific cause, not Temporal's wrapper."""

from __future__ import annotations

from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from jhin_workflows.agent_task.workflows import _failure_message


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


def test_unwraps_application_error_message() -> None:
    cause = ApplicationError("openai: HTTP 429: quota exceeded", type="provider")
    assert _failure_message(_activity_error(cause)) == "openai: HTTP 429: quota exceeded"


def test_plain_exception_keeps_its_text() -> None:
    assert _failure_message(RuntimeError("snapshot missing")) == "snapshot missing"


def test_generic_wrapper_without_cause_keeps_generic_text() -> None:
    assert _failure_message(_activity_error(None)) == "Activity task failed"  # type: ignore[arg-type]


def test_message_is_bounded() -> None:
    assert len(_failure_message(RuntimeError("x" * 5000))) == 2000
