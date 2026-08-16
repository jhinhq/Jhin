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
from typing import Any

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
            values = list(self._values)
        for value in values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively scrub strings inside common container types."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            redacted = [self.redact_value(item) for item in value]
            return tuple(redacted) if isinstance(value, tuple) else redacted
        return value


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
    return {key: _global_redactor.redact_value(value) for key, value in event_dict.items()}
