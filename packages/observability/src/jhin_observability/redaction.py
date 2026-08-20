"""Bounded structural redaction for all JSON-v1 log values."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from structlog.typing import EventDict, WrappedLogger

LOG_SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"
MAX_LOG_DEPTH = 8
MAX_LOG_ITEMS = 64
MAX_LOG_STRING = 2_000
MAX_TRACEBACK_FRAMES = 32
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "api_key",
        "private_key",
        "dsn",
        "prompt",
        "completion",
        "sql",
        "tool_input",
        "tool_output",
        "request_body",
        "response_body",
        "webhook_payload",
        "secret_env",
    }
)
SENSITIVE_KEY_SUFFIXES = (
    "_authorization",
    "_cookie",
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_private_key",
    "_dsn",
)


def is_sensitive_key(key: str) -> bool:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key.strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return value[:MAX_LOG_STRING]
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:MAX_LOG_STRING]


def structural_redaction(value: object, *, _depth: int = 0) -> object:
    if _depth >= MAX_LOG_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:MAX_LOG_ITEMS]:
            try:
                safe_key = str(key)[:128]
            except Exception:
                safe_key = "[UNSUPPORTED]"
            result[safe_key] = (
                REDACTED
                if is_sensitive_key(safe_key)
                else structural_redaction(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [structural_redaction(item, _depth=_depth + 1) for item in value[:MAX_LOG_ITEMS]]
    if isinstance(value, str):
        candidate = sanitize_url(value) if "://" in value else value
        return candidate[:MAX_LOG_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return str(value)[:MAX_LOG_STRING]
    except Exception:
        return "[UNSUPPORTED]"


def structural_redaction_processor(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> EventDict:
    """Apply structural bounds while values are still typed objects."""
    redacted = structural_redaction(event_dict)
    return redacted if isinstance(redacted, dict) else {}
