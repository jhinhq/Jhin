"""Deterministic secret / sensitive-data screening for memory candidates.

Two tiers:

- **reject**: credential material (API keys, bearer tokens, authorization
  headers, private keys, DSNs carrying a password). Storing a redacted copy
  of "the API key is …" has no value and the surrounding text is usually
  just the credential's label, so the whole candidate is dropped.
- **redact**: ``password: hunter2`` style assignments are replaced with a
  marker and the record is stored with ``sensitivity=redacted``.

The patterns are intentionally conservative and string-based — no model
involvement (plan: deterministic policy).
"""

from __future__ import annotations

import re

from jhin_memory.types import ScreeningResult

REDACTION_MARKER = "[REDACTED]"

_REJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization_header", re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*")),
    ("basic_auth_header", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}=*")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "dsn_with_password",
        re.compile(r"(?i)\b[a-z][a-z0-9+\-.]*://[^/\s:@]+:[^/\s@]+@[^\s]+"),
    ),
    (
        "generic_key_assignment",
        re.compile(
            r"(?i)\b(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|client[_\-]?secret|"
            r"private[_\-]?key|auth[_\-]?token|x-api-key)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"
        ),
    ),
)

_REDACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "password_assignment",
        re.compile(r"(?i)\b(passw(?:or)?d|passphrase|pin)\b(\s*(?:is|[:=])\s*)(['\"]?)(\S+)\3"),
    ),
)


def screen_content(content: str) -> ScreeningResult:
    """Classify ``content`` as clean, redacted, or rejected."""
    reasons: list[str] = []
    for name, pattern in _REJECT_PATTERNS:
        if pattern.search(content):
            reasons.append(f"secret:{name}")
    if reasons:
        return ScreeningResult(content="", rejected=True, reasons=tuple(reasons))

    redacted = content
    for name, pattern in _REDACT_PATTERNS:
        new_value, count = pattern.subn(rf"\1\2{REDACTION_MARKER}", redacted)
        if count:
            reasons.append(f"redacted:{name}")
            redacted = new_value
    if reasons:
        return ScreeningResult(content=redacted, redacted=True, reasons=tuple(reasons))
    return ScreeningResult(content=content)


def contains_secret(text: str) -> bool:
    """Cheap check used by tools/API to refuse obviously secret input."""
    return screen_content(text).rejected
