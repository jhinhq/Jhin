"""Sanitization for tool inputs/outputs before persistence (plan 6.15, 21.8-9).

Two defenses, applied in order:

1. secret redaction — every string is scrubbed through the process-wide
   :class:`jhin_secrets.redaction.SecretRedactor`, which knows every secret
   value decrypted in this process;
2. size limits — oversized values are truncated with an explicit marker, so
   a hostile tool output cannot flood the database, the prompt window, or
   the UI.
"""

from __future__ import annotations

import json
import math
from typing import Any

from jhin_secrets.redaction import SecretRedactor, get_redactor

# Plan 21.8: tool outputs are size-limited. Caps are generous for real work
# but hard: one string leaf, and the serialized document as a whole.
MAX_STRING_CHARS = 8_192
MAX_DOCUMENT_BYTES = 32_768

TRUNCATION_MARKER = "…[truncated]"
_MIN_KEY_CAP_CHARS = len(TRUNCATION_MARKER) + 8


class StrictJSONError(ValueError):
    """The document uses syntax Python accepts but RFC JSON forbids."""


def _reject_nonstandard_json_constant(value: str) -> None:
    raise StrictJSONError(f"non-standard JSON constant is not allowed: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _decode_finite_json_float(value: str) -> float:
    decoded = float(value)
    if not math.isfinite(decoded):
        raise StrictJSONError("non-finite JSON number is not allowed")
    return decoded


def strict_json_loads(document: str) -> Any:
    """Decode RFC-compatible JSON without non-finite numbers or duplicate keys."""
    return json.loads(
        document,
        parse_constant=_reject_nonstandard_json_constant,
        parse_float=_decode_finite_json_float,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def _truncate_string(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _unique_bounded_key(
    key: str,
    existing: dict[str, Any],
    max_string_chars: int,
) -> str:
    """Keep every value when redaction/truncation makes mapping keys collide."""
    if key not in existing:
        return key
    collision = 2
    while True:
        suffix = f"#{collision}"
        candidate = f"{key[: max_string_chars - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
        collision += 1


def _sanitize_value(value: Any, redactor: SecretRedactor, max_string_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_string(redactor.redact_text(value), max_string_chars)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            # Keys are provider-controlled strings too. Redact the complete
            # key before applying the cap so truncation cannot strand a
            # recognizable prefix of a credential.
            bounded_key = _truncate_string(redactor.redact_text(str(key)), max_string_chars)
            safe_key = _unique_bounded_key(
                bounded_key,
                sanitized,
                max_string_chars,
            )
            sanitized[safe_key] = _sanitize_value(item, redactor, max_string_chars)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, redactor, max_string_chars) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Unknown types (should not appear in JSON payloads) become redacted text.
    return _truncate_string(redactor.redact_text(str(value)), max_string_chars)


def sanitize_payload(
    payload: dict[str, Any],
    *,
    redactor: SecretRedactor | None = None,
    max_string_chars: int = MAX_STRING_CHARS,
    max_document_bytes: int = MAX_DOCUMENT_BYTES,
) -> dict[str, Any]:
    """Redact and size-cap a JSON-shaped payload.

    If the document is still too large after per-string truncation, the whole
    payload is replaced by a marker object carrying a redacted preview — the
    original is intentionally lost (plan 21.8).
    """
    if max_string_chars < _MIN_KEY_CAP_CHARS:
        raise ValueError(f"max_string_chars must be at least {_MIN_KEY_CAP_CHARS} characters")
    active_redactor = redactor if redactor is not None else get_redactor()
    sanitized = _sanitize_value(payload, active_redactor, max_string_chars)
    serialized = json.dumps(sanitized, ensure_ascii=False, default=str)
    if len(serialized.encode()) <= max_document_bytes:
        result: dict[str, Any] = sanitized
        return result
    preview_chars = max(256, max_document_bytes // 16)
    return {
        "truncated": True,
        "original_size_bytes": len(serialized.encode()),
        "preview": serialized[:preview_chars] + TRUNCATION_MARKER,
    }
