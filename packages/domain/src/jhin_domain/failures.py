"""What a run failure says to the person who was waiting for the answer.

A failed run records two things: a short ``error_code`` and an
``error_message`` written for whoever has to debug it. Both were shown
verbatim, so a chat that had been speaking English all afternoon suddenly
said

    Run failed: tool call a34dd1dc-ba6e-506e-a509-167d07efc02d execution
    outcome is unknown; manual reconciliation is required

which is three pieces of internal vocabulary and an identifier, addressed to
nobody who was in the room. This module is the other half of that record: one
sentence per failure *class*, in the product's own voice, with the identifier
lifted out of the prose and kept beside it for the support conversation that
may follow.

Rendered here, on the server, and next to :mod:`jhin_domain.activity` for the
same reason it is: the chat, the company activity feed and the attention inbox
all describe the same failure, and a copy of this vocabulary kept in
TypeScript would drift until two surfaces disagreed about what went wrong.

**The code is the only thing that chooses a sentence.** ``error_message`` is
free text assembled from provider errors and container output; it is carried
through as ``detail`` for the surfaces that show it, never parsed for meaning,
and never used to pick words. An unrecognized code falls back to the honest
generic rather than to the raw string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: How much of the raw failure text survives into ``detail``. Long enough for
#: a provider's sentence, short enough that a failure card cannot become a
#: stack trace. The message is bounded at 2 000 characters where it is
#: written; this is what a person is asked to read.
MAX_DETAIL_CHARS = 400

# The reference a support conversation actually starts from: the tool call,
# run, or approval id that Jhin's own failure sentences name inline. Matched
# rather than parsed — this module never reads the message for meaning, only
# for the identifier a person would otherwise have to copy out of a sentence
# written for somebody else.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# The transcript's own framing, and not a word of the failure. The agent
# worker records a failed run in the chat as ``f"Run {status}: {message}"``,
# and that row is left exactly as written because it is the record. When the
# row is all a surface has to build a notice from, the status line comes off
# first: a card whose heading already says the agent could not finish does not
# also need "Run failed:", and that prefix is the internal half of the very
# sentence this module exists to replace.
_RUN_STATUS_PREFIX = re.compile(r"^run\s+[a-z_]+:\s*", re.IGNORECASE)

# One sentence per failure class. Present tense, no identifiers, no component
# names: "the tool worker" and "the snapshot activity" are true and useless.
# Each says what stopped and — where the cause is genuinely outside the
# person's reach — that it was not something they did.
_SUMMARIES: dict[str, str] = {
    # The interrupted-mid-step case this vocabulary was written for. Note what
    # it does *not* claim: not that the step failed, and not that it succeeded.
    "tool_execution_unknown": (
        "A step was cut short before it could report back, so there is no record "
        "of whether it finished."
    ),
    "tool_invocation_mismatch": (
        "A step could not be matched to the work it belonged to, so the run stopped "
        "rather than carry on from something it could not vouch for."
    ),
    "tool_step_manifest_not_lossless": (
        "A step's own record of itself did not add up, so the run stopped rather "
        "than carry on from something it could not vouch for."
    ),
    "step_failed": "A step did not complete.",
    # A turn that stopped before there was anything to stop: the work never
    # reached the agent, so there is no run, no step, and nothing anyone has
    # to go and check. Saying that plainly is what makes trying again the
    # obvious next move rather than a gamble.
    "workflow_start_failed": "This never reached the agent, so nothing has run yet.",
    "snapshot_failed": "The agent's configuration could not be read, so it never started.",
    "max_steps_exceeded": "The agent used up the steps allowed for one turn before finishing.",
    "budget_exceeded": "This workspace has reached its model spending limit.",
    "insufficient_funds": "The model provider declined the request over billing.",
    "model_incompatible_request": "The model profile does not accept a request of this shape.",
    "delegation_failed": "Handing part of this to a colleague did not work.",
    "review_wait_unsupported": "This run was waiting on a review it cannot resume from.",
    "review_resolution_failed": "The review decision on this work could not be applied.",
    "approval_resolution_failed": "Your decision on the approval could not be applied.",
}

DEFAULT_SUMMARY = "The run stopped before it finished."

# Codes whose ``error_message`` Jhin wrote itself. For these the summary above
# is the same fact in better words, so repeating the original underneath would
# only put the vocabulary back on screen — the identifier inside it is kept as
# ``reference`` instead. Every other code carries text from outside (a
# provider's sentence, a container's stderr, a colleague's refusal), which is
# the actual information and is kept.
_SELF_DESCRIBED = frozenset(
    {
        "tool_execution_unknown",
        "tool_invocation_mismatch",
        "tool_step_manifest_not_lossless",
        "max_steps_exceeded",
        "workflow_start_failed",
    }
)


@dataclass(frozen=True, slots=True)
class FailureNotice:
    """One failure, ready to put in front of a person.

    ``summary`` is always safe to show and always says something. ``detail``
    and ``reference`` are both possibly empty, and a surface that shows them
    must survive that — ``detail`` because some failures have nothing to add
    beyond the summary, ``reference`` because most have no identifier in them.
    """

    #: The internal code, unchanged. Not for the headline; for the support
    #: conversation, the metrics, and the client that wants to special-case
    #: one class (an out-of-credit card that links to Models, say).
    code: str
    #: What happened, in one sentence a person can read.
    summary: str
    #: The failure's own account of itself where that adds something the
    #: summary does not: a provider's message, a command's stderr tail.
    detail: str
    #: The identifier support would ask for, lifted out of the prose so it can
    #: be shown quietly rather than first.
    reference: str


def failure_notice(code: str | None, message: str = "") -> FailureNotice:
    """Turn a run's ``(error_code, error_message)`` into readable copy.

    Pure, and total: every input produces a notice with a real sentence in it,
    because the surfaces that call this have a person waiting and no second
    thing to show.
    """
    normalized = (code or "").strip()
    raw = _RUN_STATUS_PREFIX.sub("", (message or "").strip())
    summary = _SUMMARIES.get(normalized, DEFAULT_SUMMARY)
    found = _UUID.search(raw)
    reference = found.group(0).lower() if found else ""
    detail = "" if normalized in _SELF_DESCRIBED else raw[:MAX_DETAIL_CHARS].strip()
    return FailureNotice(code=normalized, summary=summary, detail=detail, reference=reference)


__all__ = ["DEFAULT_SUMMARY", "MAX_DETAIL_CHARS", "FailureNotice", "failure_notice"]
