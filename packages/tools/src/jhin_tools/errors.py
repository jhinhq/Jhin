"""Typed executor failures whose effect boundary is explicit."""

from __future__ import annotations

import re

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


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
    ) -> None:
        if not _ERROR_CODE_RE.fullmatch(code):
            raise ValueError("tool execution error code is invalid")
        if not isinstance(side_effect_possible, bool):
            raise TypeError("side_effect_possible must be a boolean")
        super().__init__(message)
        self.code = code
        self.side_effect_possible = side_effect_possible


__all__ = ["ToolExecutionError"]
