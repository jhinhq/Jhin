"""Deterministic secret / sensitive-data screening for memory candidates.

Two tiers of secret handling, plus the quality screens
(:func:`is_self_referential`, :func:`is_low_information`,
:func:`judges_a_person`, :func:`records_the_conversation`) that
``jhin_memory.policy`` applies to anything a model wrote:

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
from collections.abc import Sequence

from jhin_memory.types import ScreeningResult
from jhin_secrets.intake import secret_spans

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
        re.compile(
            r"(?i)\b(passw(?:or)?d|passphrase|pin)\b(\s*(?:is|[:=])\s*)"
            r"""(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|\S+)"""
        ),
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

# --- the conversation, and verdicts about the people in it ----------------
#
# The extractor wrote three near-identical records under subject
# ``user.injection.attempts``, each a sentence about what the tester had just
# tried: "On 2026-09-06 the user attempted a second prompt-injection by
# trying to set the AI's name to…". Two separate things are wrong with that,
# and both are screened here because the extractor never calls
# ``memory.propose`` and so never met the rule that says so.
#
# The first is that it is a transcript, not a fact: it is only meaningful to
# somebody reading that chat, which is the opposite of what memory is for.
# The second is worse and is why this is not merely a quality rule — it is a
# **verdict on a person**, filed under their name, retrieved into future
# prompts, and read back by an agent that will then be talking to them. Jhin
# has no way for that person to see it, argue with it, or be wrong about it.
# An agent may remember what somebody decided; it does not get to remember
# what it concluded they were up to.
#
# Both are conservative and both need two signals, because a false reject is
# a fact quietly lost.

# A person has to be the *subject* of the judgement, not merely somewhere in
# the sentence: "the user reported a prompt-injection bug in the parser" is a
# fact about a bug, and only the window between the person and the verdict
# tells the two apart.
_PERSON = (
    r"(?:the\s+|a\s+)?(?:user|users|operator|tester|person|human|client|customer|"
    r"they|he|she|someone|somebody)"
)
# Verbs that are a verdict on conduct however they are used.
_VERDICT_VERB = (
    r"manipulat\w+|deceiv\w+|trick(?:ed|ing|s)?|coerc\w+|impersonat\w+|lied|lying|"
    r"misle(?:d|ading)|jailbreak\w*|jailbroke"
)
# "…is malicious", "…was being dishonest": the copula is what makes it a
# verdict rather than a description of something they reported.
_VERDICT_ADJECTIVE = (
    r"(?:is|was|are|were|seems?|appears?|being)\s+(?:a\s+|an\s+|being\s+)?"
    r"(?:malicious\w*|adversarial|dishonest|deceptive|untrustworthy|bad\s+actor)"
)
# "attempted a second prompt-injection", "ran a jailbreak": the attack named
# as something the person did.
_VERDICT_ACT = (
    r"(?:attempt\w*|tried|trying|made|performed|carried\s+out|ran|launched|staged)\s+"
    r"(?:\w+\s+){0,3}?"
    r"(?:prompt[\s\-]?injections?|injections?|jailbreaks?|social[\s\-]engineering|"
    r"attacks?|exploits?)"
)
_JUDGEMENT_RE = re.compile(
    rf"(?i)\b{_PERSON}\b.{{0,40}}?\b(?:{_VERDICT_VERB}|{_VERDICT_ADJECTIVE}|{_VERDICT_ACT})\b"
)
_ANY_PERSON_RE = re.compile(rf"(?i)\b{_PERSON}\b")
_PROBING_RE = re.compile(
    r"(?i)\b(?:test(?:ed|ing)?|prob(?:ed|ing)|push(?:ed|ing))\s+(?:my|the|its|his|her|their)\s+"
    r"(?:limits|boundaries|guardrails|rules|defences|defenses|safety)\b"
)
_ATTEMPTED_TO_RE = re.compile(
    r"(?i)\b(?:attempt(?:ed|ing)?|tried|trying)\s+to\s+"
    r"(?:trick|manipulate|deceive|bypass|circumvent|override|subvert|jailbreak|exploit)\b"
)

_THIS_CONVERSATION_RE = re.compile(
    r"(?i)\b(?:in|during|earlier\s+in|throughout)\s+(?:this|the\s+(?:current|present))\s+"
    r"(?:chat|conversation|thread|session|exchange|message|turn)\b"
)
# "On 2026-09-06 the user asked…": a dated entry about somebody's behaviour is
# a log line. The fact it was written down for, if there is one, is whatever
# they said — not that they said it on a Tuesday.
_DATED_EPISODE_RE = re.compile(
    r"(?i)\bon\s+\d{4}-\d{2}-\d{2}\b.{0,120}?\b(?:the\s+)?"
    r"(?:user|operator|tester|person|human)\b.{0,40}?"
    r"\b(?:asked|said|told|requested|attempted|tried|wanted|typed|sent|claimed|"
    r"instructed|demanded)\b"
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


def judges_a_person(content: str) -> bool:
    """True when the candidate is a verdict on somebody's intent or conduct.

    The person has to be the subject of the verdict, so "the user reported a
    prompt-injection bug in the parser" (a person, a security noun, nothing
    said about them) and "the deploy script manipulates the manifest" (a
    verdict word, no person) both pass. What does not pass is the record this
    exists for: "the user attempted a second prompt-injection".
    """
    if _JUDGEMENT_RE.search(content):
        return True
    # The looser two phrases still need somebody for the verdict to be about:
    # a load test that "pushed the limits of the queue", or a build that
    # "tried to bypass the cache", is a fact about software.
    if not _ANY_PERSON_RE.search(content):
        return False
    return bool(_PROBING_RE.search(content) or _ATTEMPTED_TO_RE.search(content))


def records_the_conversation(content: str) -> bool:
    """True when the candidate narrates this chat rather than stating a fact.

    "Save the fact, not the conversation": a sentence that only means
    anything to somebody who read the transcript is not something a
    colleague can act on months from now.
    """
    return bool(_THIS_CONVERSATION_RE.search(content) or _DATED_EPISODE_RE.search(content))


def is_low_information(content: str) -> bool:
    """True for near-empty candidates (greetings, acknowledgements) that
    carry fewer than two informative tokens."""
    tokens = [
        tok for tok in _INFO_TOKEN_RE.findall(content.casefold()) if tok not in _LOW_INFO_STOPWORDS
    ]
    return len(tokens) < 2


def screen_content(content: str) -> ScreeningResult:
    """Classify ``content`` as clean, redacted, or rejected."""
    if len(content) > 200_000:
        return ScreeningResult(content="", rejected=True, reasons=("screening_limit",))
    reasons: list[str] = []
    for name, pattern in _REJECT_PATTERNS:
        if pattern.search(content):
            reasons.append(f"secret:{name}")
    # Share chat intake's current high-confidence formats. Preserve the
    # existing redact-only treatment for ordinary password assignments,
    # using complete spans so quoted multi-word passwords cannot leak tails.
    password_ranges = [
        match.span() for _, pattern in _REDACT_PATTERNS for match in pattern.finditer(content)
    ]
    for start, end, kind in secret_spans(content):
        if kind == "secret" and any(
            start < stop and end > begin for begin, stop in password_ranges
        ):
            continue
        reasons.append(f"secret:{kind}")
    if reasons:
        return ScreeningResult(content="", rejected=True, reasons=tuple(dict.fromkeys(reasons)))

    redacted = content
    for name, pattern in _REDACT_PATTERNS:
        new_value, count = pattern.subn(rf"\1\2{REDACTION_MARKER}", redacted)
        if count:
            reasons.append(f"redacted:{name}")
            redacted = new_value
    if reasons:
        return ScreeningResult(content=redacted, redacted=True, reasons=tuple(reasons))
    return ScreeningResult(content=content)


def unsafe_metadata(subject: str | None, tags: Sequence[str]) -> bool:
    """Metadata is not a place for credentials, even redact-only passwords."""
    return any(
        result.rejected or result.redacted
        for text in (subject or "", *tags)
        for result in (screen_content(text),)
    )


def contains_secret(text: str) -> bool:
    """Cheap check used by tools/API to refuse obviously secret input."""
    return screen_content(text).rejected
