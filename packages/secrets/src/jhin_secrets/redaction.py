"""Secret redaction for logs and error strings (plan 13.5, invariant 48.9).

A process-wide registry of known plaintext secret values. Every decryption
registers the plaintext here, so anything that later flows through the
logging pipeline (or explicitly calls :func:`redact_text`) has those values
replaced with a placeholder before leaving the process.

The registry keeps values only in memory and never exposes iteration order or
contents; it can answer "does this string contain a known secret" and rewrite
strings, nothing else.
"""

from __future__ import annotations

import threading
from collections.abc import MutableMapping
from typing import Any, cast

REDACTED = "[REDACTED]"

# Values shorter than this are never registered: redacting e.g. "a" would
# shred unrelated log text and such strings are not meaningful secrets.
_MIN_SECRET_LENGTH = 6


class SecretRedactor:
    """Thread-safe registry of known secret values with string scrubbing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, plaintext: str) -> None:
        if len(plaintext) < _MIN_SECRET_LENGTH:
            return
        with self._lock:
            self._values.add(plaintext)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def redact_text(self, text: str) -> str:
        with self._lock:
            # Replace the longest value first so an overlapping short secret
            # cannot partially rewrite a longer credential and expose its
            # unmatched suffix.
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def stream_chunk(self, text: str, *, final: bool = False) -> tuple[str, str]:
        """Return safe append-only output and an undecided secret prefix.

        Callers prepend the returned pending suffix to the next raw chunk.
        A prefix is withheld, not exposed and later erased from a transcript.
        """
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        pieces: list[str] = []
        index = 0
        while index < len(text):
            remaining = text[index:]
            complete = next((v for v in values if remaining.startswith(v)), None)
            partial = any(v.startswith(remaining) and len(v) > len(remaining) for v in values)
            if partial and not final:
                return "".join(pieces), remaining
            if complete:
                pieces.append(REDACTED)
                index += len(complete)
            elif partial:
                pieces.append(REDACTED)
                break
            else:
                pieces.append(text[index])
                index += 1
        return "".join(pieces), ""

    @property
    def max_secret_length(self) -> int:
        """Overlap needed when a bounded stream buffer drops its oldest text."""
        with self._lock:
            return max((len(value) for value in self._values), default=0)

    def redact_partial_text(self, text: str, *, clipped_start: bool = False) -> str:
        """Scrub a live snapshot, including incomplete secrets at its edges.

        Snapshot consumers replace prior text; they must never append these
        strings as deltas. A later snapshot can resolve a withheld prefix.
        """
        with self._lock:
            values = tuple(self._values)
        spans: list[tuple[int, int]] = []
        for value in values:
            offset = text.find(value)
            while offset >= 0:
                spans.append((offset, offset + len(value)))
                offset = text.find(value, offset + 1)
            for size in range(min(len(text), len(value) - 1), 0, -1):
                if text.endswith(value[:size]):
                    spans.append((len(text) - size, len(text)))
                    break
            if clipped_start:
                for size in range(min(len(text), len(value) - 1), 0, -1):
                    if text.startswith(value[-size:]):
                        spans.append((0, size))
                        break
        # Match against the original string: replacing a shorter secret first
        # can destroy the unfinished prefix of a longer overlapping secret.
        # Union also prevents an edge span cutting a complete secret in half.
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        parts: list[str] = []
        offset = 0
        for start, end in merged:
            parts.extend((text[offset:start], REDACTED))
            offset = end
        parts.append(text[offset:])
        return "".join(parts)

    def redact_value(self, value: Any) -> Any:
        """Recursively scrub strings inside common container types."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                try:
                    rendered_key = str(key)
                except Exception:
                    rendered_key = "[unsupported mapping key]"
                safe_key = self.redact_text(rendered_key)
                if safe_key in redacted:
                    # Distinct secret-bearing keys commonly collapse to the
                    # same marker. Preserve every value with an encounter-order
                    # suffix; never derive a suffix from secret material.
                    collision = 2
                    while f"{safe_key}#{collision}" in redacted:
                        collision += 1
                    safe_key = f"{safe_key}#{collision}"
                redacted[safe_key] = self.redact_value(item)
            return redacted
        if isinstance(value, (list, tuple)):
            redacted_items = [self.redact_value(item) for item in value]
            return tuple(redacted_items) if isinstance(value, tuple) else redacted_items
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        # JSON renderers stringify unknown objects only *after* processors
        # have run. Returning the original object would therefore let a
        # secret-bearing ``__repr__`` or ``__structlog__`` bypass redaction.
        try:
            rendered = str(value)
        except Exception:
            rendered = "[unsupported log value]"
        return self.redact_text(rendered)


_global_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    """The process-wide redactor shared by the secret store and log pipeline."""
    return _global_redactor


def redact_text(text: str) -> str:
    return _global_redactor.redact_text(text)


def redact_event_dict(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    """structlog processor: scrub known secret values from every log record."""
    return cast(dict[str, Any], _global_redactor.redact_value(dict(event_dict)))
