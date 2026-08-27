"""A refused memory has to come back as something the agent can act on.

The tool returned only machine reason codes, so an agent asked to remember
something for the team told the person its request was "rejected under the
non-amplification policy" -- true, useless, and not a sentence anyone can do
anything with. The refusal itself is correct: a private chat is visible to two
people, and a memory may never be broader than its source.
"""

from __future__ import annotations

import pytest

from jhin_tools.memory import _propose_detail


def test_a_private_chat_says_what_would_have_worked() -> None:
    detail = _propose_detail("reject", "none", ["non_amplification"])
    assert "agent" in detail
    assert "Memories" in detail
    # The words a person should never be shown.
    assert "non_amplification" not in detail
    assert "scope_exceeds" not in detail


@pytest.mark.parametrize(
    "reason",
    [
        "non_amplification",
        "insufficient_authority",
        "no_team_for_scope",
        "low_information",
        "self_reference",
        "source_internal",
        "contradiction",
    ],
)
def test_every_actionable_refusal_is_a_sentence(reason: str) -> None:
    detail = _propose_detail("reject", "none", [reason])
    assert detail.endswith(".")
    assert reason not in detail
    assert detail[0].isupper()


def test_an_unmapped_refusal_still_is_not_a_code() -> None:
    detail = _propose_detail("reject", "none", ["something_new"])
    assert "something_new" not in detail
    assert detail.endswith(".")


def test_a_stored_memory_says_so() -> None:
    assert _propose_detail("accept", "active", []) == "Remembered."
    assert "review" in _propose_detail("accept", "pending", ["workspace_promotion_requires_review"])
