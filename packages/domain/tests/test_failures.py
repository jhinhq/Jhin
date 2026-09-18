"""The run-failure → sentence mapping the product says instead of its notes."""

import pytest

from jhin_domain import DEFAULT_SUMMARY, UNRECONCILED_TOOL_STATUSES, ToolCallStatus, failure_notice
from jhin_domain.failures import _SELF_DESCRIBED, _SUMMARIES, MAX_DETAIL_CHARS

# Verbatim, from the run that prompted all of this.
INTERRUPTED = (
    "tool call a34dd1dc-ba6e-506e-a509-167d07efc02d execution outcome is unknown; "
    "manual reconciliation is required"
)


def test_the_interrupted_step_is_said_in_the_products_own_words() -> None:
    notice = failure_notice("tool_execution_unknown", INTERRUPTED)

    assert notice.summary == (
        "A step was cut short before it could report back, so there is no record "
        "of whether it finished."
    )
    # The three things that made the original unreadable are all gone from the
    # sentence a person reads: the internal noun phrase, the instruction
    # addressed to an operator, and the identifier leading the line.
    assert "reconciliation" not in notice.summary
    assert "tool call" not in notice.summary
    assert "a34dd1dc" not in notice.summary
    assert notice.detail == ""


def test_the_identifier_survives_beside_the_sentence() -> None:
    """Support still needs the id; it just stops being the greeting."""
    assert failure_notice("tool_execution_unknown", INTERRUPTED).reference == (
        "a34dd1dc-ba6e-506e-a509-167d07efc02d"
    )
    assert failure_notice("tool_execution_unknown", INTERRUPTED.upper()).reference == (
        "a34dd1dc-ba6e-506e-a509-167d07efc02d"
    )


def test_a_provider_saying_something_useful_keeps_saying_it() -> None:
    """The fix is for Jhin's own vocabulary, not for everyone else's words.

    A quota message names the actual problem; replacing it with "a step did
    not complete" would be the same mistake in the opposite direction.
    """
    notice = failure_notice("step_failed", "openai: HTTP 429: You exceeded your current quota")

    assert notice.detail == "openai: HTTP 429: You exceeded your current quota"
    assert notice.summary == "A step did not complete."


def test_every_self_described_code_has_a_sentence_of_its_own() -> None:
    """Dropping the original text is only safe where the summary replaces it."""
    assert set(_SUMMARIES) >= _SELF_DESCRIBED


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (None, ""),
        ("", ""),
        ("something_nobody_has_written_copy_for", ""),
        ("  ", "   "),
    ],
)
def test_a_failure_always_says_something(code: str | None, message: str) -> None:
    """A person is waiting and there is no second thing to show them."""
    notice = failure_notice(code, message)

    assert notice.summary == DEFAULT_SUMMARY
    assert notice.summary.strip()


def test_an_unknown_code_still_carries_the_text_it_had() -> None:
    """Falling back to the generic sentence must not throw away the evidence."""
    notice = failure_notice("brand_new_code", "the disk filled up")

    assert notice.summary == DEFAULT_SUMMARY
    assert notice.detail == "the disk filled up"


def test_detail_is_bounded_so_a_failure_cannot_become_a_stack_trace() -> None:
    notice = failure_notice("step_failed", "x" * 5_000)

    assert len(notice.detail) == MAX_DETAIL_CHARS


def test_no_summary_carries_an_identifier_or_a_component_name() -> None:
    """These sentences are read by people who do not run the platform."""
    for code, summary in _SUMMARIES.items():
        assert summary.endswith("."), code
        assert "_" not in summary, code
        assert code not in summary, code


def test_the_unaccountable_statuses_are_the_gateways_own() -> None:
    """The retry decision reads the tool layer's conclusion, not a second one.

    ``claimed`` is deliberately absent: the gateway proves nothing was
    dispatched there and re-executes such a call itself. Nor is a call whose
    tool declares a repeat safe (``redispatch_is_safe``) — recovery re-runs
    that one, so it ends terminal. What is left here is the residue the
    platform itself declined to repeat, and if either of those rules moves,
    this set moves with it rather than beside it.
    """
    assert {
        ToolCallStatus.EXECUTING,
        ToolCallStatus.EXECUTION_UNKNOWN,
    } == UNRECONCILED_TOOL_STATUSES
    assert ToolCallStatus.CLAIMED not in UNRECONCILED_TOOL_STATUSES
    assert ToolCallStatus.COMPLETED not in UNRECONCILED_TOOL_STATUSES
