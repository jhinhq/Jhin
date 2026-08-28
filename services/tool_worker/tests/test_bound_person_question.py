"""A step that asked somebody stops there.

The rest of a manifest is work whose premise is the answer nobody has given
yet, so ``_bound_result`` tells the workflow to stop scheduling. This is the
third caller of ``asked_question_id`` — the projection's suppression and its
lift are the other two, and if the three ever disagree the model is left with
a tool call that never gets a result.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from jhin_tool_worker.activities import _bound_result
from jhin_tools import GatewayOutcome


def outcome(**overrides: Any) -> GatewayOutcome:
    values: dict[str, Any] = {
        "status": "executed",
        "tool_call_id": uuid4(),
        "tool_name": "organization.ask_person",
        "risk": "write",
        "decision_code": "granted",
        "decision_reason": "executed through the tool gateway",
        "sanitized_input": {},
        "sanitized_output": {"status": "asked", "question_id": str(uuid4()), "detail": "Asked."},
    }
    values.update(overrides)
    return GatewayOutcome(**values)


def test_an_ask_that_reached_somebody_stops_the_step() -> None:
    assert _bound_result(outcome()).stop_reason == "awaiting_person"


@pytest.mark.parametrize(
    ("sanitized_output", "why"),
    [
        (
            {"status": "already_asked", "question_id": str(uuid4()), "detail": "d"},
            "a repeat never reached anyone, so there is nothing to wait for",
        ),
        (
            {"status": "not_asked", "detail": "over budget"},
            "an over-budget refusal is useful in the same step",
        ),
        (
            {"status": "asked", "question_id": "", "detail": "d"},
            "no id means no question to park on",
        ),
    ],
)
def test_an_ask_nobody_saw_lets_the_step_carry_on(
    sanitized_output: dict[str, Any], why: str
) -> None:
    assert _bound_result(outcome(sanitized_output=sanitized_output)).stop_reason is None, why


def test_another_tool_reporting_asked_does_not_park_the_step() -> None:
    """The predicate is keyed on the tool name too: a connector returning a
    ``status`` of "asked" is talking about something else entirely."""
    result = _bound_result(
        outcome(
            tool_name="system.echo",
            sanitized_output={"status": "asked", "question_id": str(uuid4())},
        )
    )
    assert result.stop_reason is None
