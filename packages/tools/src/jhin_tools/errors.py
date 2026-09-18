"""Typed executor failures whose effect boundary is explicit."""

from __future__ import annotations

import re

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


MAX_HINT_CHARS = 400
#: How much of a failure's own account of itself survives. Long enough for a
#: provider's sentence and a git exit line, short enough that a tool failure
#: cannot become a transcript.
MAX_DETAIL_CHARS = 800
_DETAIL_SCAN_CHARS = 8_000


def bounded_detail(value: str) -> str:
    """Untrusted failure text, made safe to store and to show.

    Newlines and tabs survive because a stderr tail without them is unreadable;
    everything else a terminal or a log viewer would act on does not. The
    result is bounded here and redacted where it is stored: the gateway runs
    every failure document through :func:`jhin_tools.sanitize.sanitize_payload`,
    which is the process secret redactor, so a token that reached this string
    is removed on the way to the row.
    """
    flattened = "".join(
        character if character.isprintable() or character in "\n\t" else " "
        for character in value[:_DETAIL_SCAN_CHARS]
    )
    return flattened.strip()[:MAX_DETAIL_CHARS]


class ToolExecutionError(Exception):
    """A bounded executor failure with an explicit side-effect classification.

    ``side_effect_possible`` defaults closed: unless an executor can prove it
    failed before any external effect, a durably claimed call is reconciled as
    execution-unknown rather than as a retryable failure.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        side_effect_possible: bool = True,
        hint: str = "",
        detail: str = "",
    ) -> None:
        if not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("tool execution error code is invalid")
        if not isinstance(side_effect_possible, bool):
            raise TypeError("side_effect_possible must be a boolean")
        if not isinstance(hint, str):
            raise TypeError("hint must be a string")
        if not isinstance(detail, str):
            raise TypeError("detail must be a string")
        super().__init__(message)
        self.code = code
        self.side_effect_possible = side_effect_possible
        # Optional *static* guidance for the model (what the tool accepts and
        # how to retry). It must be fixed text chosen by the connector, never
        # a provider message, because it is the one part of a failure that
        # crosses the gateway boundary into the model's observation.
        self.hint = hint[:MAX_HINT_CHARS]
        # Optional *untrusted* account of what actually went wrong: the
        # provider's own error message, a container's stderr tail, an exit
        # code. The opposite of ``hint`` in every way except that it is
        # bounded, and it exists because a failure that says only
        # ``github_http_403`` told nobody -- not the agent, which retried the
        # same call, and not the operator, who had to read GitHub's logs to
        # learn that the installation lacked write permission. Treat it as
        # data: it is quoted back to the model as evidence, never as
        # instruction, and it is redacted where it is stored.
        self.detail = bounded_detail(detail)


__all__ = ["MAX_DETAIL_CHARS", "MAX_HINT_CHARS", "ToolExecutionError", "bounded_detail"]
