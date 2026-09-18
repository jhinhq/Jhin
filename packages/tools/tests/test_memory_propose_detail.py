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
    """It used to end at "someone has to add it on the Memories page", which
    was the only route there was. Now the agent can ask, so the sentence has
    to point at the thing it can actually do -- otherwise the model reads
    team memory as unreachable and never tries."""
    detail = _propose_detail("reject", "none", ["non_amplification"])
    assert "organization.ask_person" in detail
    assert "authorized_by_question_id" in detail
    assert "your own memory" in detail
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


def test_a_refused_self_reference_points_at_the_rename_tool() -> None:
    """The old sentence — "it describes this conversation rather than a
    durable fact" — was backwards for the case that produces it most. Being
    told your name IS durable; it is a row rather than a memory, and the
    model is instructed to relay this sentence to the person who said it."""
    detail = _propose_detail("reject", "none", ["self_reference"])
    assert "organization.identity.set_name" in detail
    assert "describes this conversation" not in detail
    # And it does not leave the agent thinking nothing can be saved.
    assert "Facts about anything other than you are fine to save." in detail
