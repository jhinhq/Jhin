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


# Facts about the agent itself ("the AI teammate's name is Bisby") are
# worthless: the agent already knows its own identity from its system prompt.
# Conservative on purpose — only clear self-reference matches.
_SELF_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(?:ai(?:\s+teammate)?|assistant|agent|teammate|bot|chatbot)(?:['\u2019]s)?\s+"
        r"(?:name\s+is|is\s+(?:named|called))\b"
    ),
    re.compile(r"(?i)\b(?:ai(?:\s+teammate)?|assistant|teammate|bot|chatbot)\s+(?:called|named)\b"),
    re.compile(r"(?i)\byour\s+name\s+is\b"),
    re.compile(r"(?i)\byou\s+are\s+(?:called|named)\b"),
    re.compile(r"(?i)\b(?:is|are)\s+an?\s+ai\s+(?:teammate|assistant|agent)\b"),
    re.compile(r"(?i)\byou\s+are\s+(?:an?\s+)?(?:ai|assistant|agent|teammate|bot|chatbot)\b"),
)
_SELF_IDENTITY_VERB_RE = re.compile(
    r"(?i)\b(?:name\s+is|is\s+named|is\s+called|is\s+an?\s+"
    r"(?:ai|assistant|agent|teammate|bot|chatbot|virtual\s+\w+))\b"
)

_INFO_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_LOW_INFO_STOPWORDS = frozenset(
    ["the", "a", "an", "is", "are", "was", "were", "it", "this", "that", "ok", "okay", "yes", "no"]
)


def is_self_referential(content: str, agent_name: str = "") -> bool:
    """True when the candidate states the agent's own identity (its name,
    that it is an AI/assistant/teammate). Facts *about other subjects* that
    merely mention the agent's name do not match."""
    for pattern in _SELF_REFERENCE_PATTERNS:
        if pattern.search(content):
            return True
    name = agent_name.strip()
    return bool(
        name
        and re.search(rf"(?i)\b{re.escape(name)}\b", content)
        and _SELF_IDENTITY_VERB_RE.search(content)
    )


def is_low_information(content: str) -> bool:
    """True for near-empty candidates (greetings, acknowledgements) that
    carry fewer than two informative tokens."""
    tokens = [
        tok for tok in _INFO_TOKEN_RE.findall(content.casefold()) if tok not in _LOW_INFO_STOPWORDS
    ]
    return len(tokens) < 2


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
