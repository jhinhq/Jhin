"""Encrypt recognizable user-provided credentials before durable chat ingress."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, overload
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models.variables import SecureInputCapture
from jhin_domain import new_uuid7
from jhin_secrets.crypto import SecretCrypto
from jhin_secrets.store import SecretStore
from jhin_secrets.variables import VariableError, canonical_admin_url, validate_value

_GHOST = re.compile(r"(?<![A-Za-z0-9])(?:[a-fA-F0-9]{24}):[a-fA-F0-9]{64}(?![A-Za-z0-9])")
_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<label>(?:api[ _-]?key|admin[ _-]?key|secret[ _-]?key|access[ _-]?token|"
    r"client[ _-]?secret|auth[ _-]?token|password|passphrase|private[ _-]?key|x-api-key))"
    r"\b\s*(?P<separator>:|=|\bis\b)\s*(?:\n\s*)?(?P<value>\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;<>`]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})")
_KNOWN = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|sb_secret_[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_PEM = re.compile(
    r"-----BEGIN (?:[A-Z ]*)PRIVATE KEY-----.*?-----END (?:[A-Z ]*)PRIVATE KEY-----", re.S
)
_DSN = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:(?P<value>[^/\s@]+)@")
_EMPTY_WORDS = {
    "missing",
    "unknown",
    "required",
    "needed",
    "not",
    "none",
    "null",
    "unset",
    "unavailable",
    "secret_ref",
}
_KEY_STATE_WORDS = frozenset(
    {
        "untouched",
        "unchanged",
        "configured",
        "saved",
        "stored",
        "deleted",
        "removed",
        "updated",
        "rotated",
        "connected",
        "encrypted",
        "protected",
        "intact",
        "available",
        "valid",
        "working",
        "active",
        "inactive",
        "verified",
    }
)
_KEY_STATE_CONTINUATION = re.compile(
    r"\s+(?:" + "|".join(sorted(_KEY_STATE_WORDS)) + r")(?=$|[\s.!?,;])", re.I
)
_GHOST_URL_DECLARATION = re.compile(
    r"(?ix)\b(?:connect\s+(?:to\s+)?ghost\s+(?:(?:at|to|using)\s+)?|"
    r"(?:reconnect(?:ing)?|verif(?:y|ying))\s+(?:to\s+)?"
    r"(?:(?:my|the|its|our|your)\s+)?(?:existing\s+)?ghost\s+"
    r"(?:(?:admin\s+)?connection\s+)?(?:at|to|using)\s+|"
    r"(?:my\s+)?ghost\s+(?:admin\s+)?(?:url|origin|site|address)\s*(?::|=|\bis\b|\bat\b)?\s*|"
    r"ghost\s+(?:is|at)\s+)(https?://[^\s<>\"`]+)"
)


def supplied_ghost_admin_urls(texts: list[str]) -> list[str]:
    """Explicit human URL declarations only; no host inference or outbound I/O."""
    supplied: dict[str, str] = {}
    rejected: set[str] = set()
    for text in texts:
        if len(text) > 200_000:
            continue
        # Formatting a bare URL is still a direct declaration. Preserve only
        # that token; quoted instructions and fenced examples remain excluded.
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(
            r"(?P<quote>[\"'`])(?P<url>https?://[^\s<>\"'`]+)(?P=quote)",
            lambda match: match.group("url"),
            text,
        )
        text = re.sub(r"```[\s\S]*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|(?<!\w)'[^']*'(?!\w)", "", text)
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
        for clause in re.split(r"(?<=[.!?])\s+|[;\n]|\s+(?:but|and)\s+", text, flags=re.I):
            prose = re.sub(r"https?://[^\s<>\"`]+", "", clause)
            negative = bool(
                re.search(
                    r"\b(?:no|not|never|cannot|can't|don't|doesn't|without|avoid|forbid|stop|unless)\b",
                    prose.replace("\u2019", "'"),
                    re.I,
                )
            )
            ambiguous = bool(
                re.search(
                    r"\b(?:if|either|or|maybe|perhaps|might|whether|example|quoted?|said|says)\b",
                    prose,
                    re.I,
                )
            )
            candidates = (
                re.findall(r"https?://[^\s<>\"`]+", clause)
                if negative
                else _GHOST_URL_DECLARATION.findall(clause)
            )
            if ambiguous and not negative:
                continue
            for raw in candidates:
                value = raw.rstrip(".,;)")
                try:
                    canonical = canonical_admin_url(value)
                except VariableError:
                    continue
                if negative:
                    rejected.add(canonical)
                else:
                    supplied[canonical] = value
    return [value for key, value in supplied.items() if key not in rejected]


def secret_spans(text: str) -> list[tuple[int, int, str]]:
    """Return disjoint value spans; recognized Ghost envelopes win overlaps.

    Deliberately never returns an excerpt. Callers must not log matches/text.
    Detection supplements explicit secure input, which also handles arbitrary
    credentials without a recognizable format.
    """
    found: list[tuple[int, int, str]] = []
    for pattern, kind, group in (
        (_GHOST, "ghost_admin_key", 0),
        (_PEM, "private_key", 0),
        (_KNOWN, "api_key", 0),
        (_BEARER, "access_token", "value"),
        (_DSN, "password", "value"),
        (_ASSIGNMENT, "secret", "value"),
    ):
        for match in pattern.finditer(text):
            start, end = match.span(group)
            value = text[start:end]
            if (
                pattern is _ASSIGNMENT
                and re.sub(r"[ _-]", "", match.group("label")).lower()
                in {"apikey", "adminkey", "privatekey"}
                and match.group("separator").lower() == "is"
                and (
                    value.rstrip(".!?").lower() in _KEY_STATE_WORDS
                    or (
                        value.lower() in {"still", "currently"}
                        and _KEY_STATE_CONTINUATION.match(text, end) is not None
                    )
                )
            ):
                # A status sentence is not a supplied value. Quoted values,
                # colon/equal assignments and passwords still reach capture.
                # Adverbs alone are not statuses: require a known following word.
                continue
            if value.startswith(("'", '"')) and value[-1:] == value[:1]:
                start, end = start + 1, end - 1
                value = text[start:end]
            # Avoid treating ordinary missing-input prose as a credential.
            if (
                value.rstrip(".!?").lower() in _EMPTY_WORDS
                or value.startswith(("[secure_input:", "secure_input:"))
                or text.startswith(("[secure input]", "[REDACTED"), start)
            ):
                continue
            if any(
                start < previous_end and end > previous_start
                for previous_start, previous_end, _ in found
            ):
                continue
            found.append((start, end, kind))
    return sorted(found)


def safe_input_text(text: str) -> str:
    """Use before constructing titles or receipts; ciphertext capture follows."""
    for start, end, _kind in reversed(secret_spans(text)):
        text = text[:start] + "[secure input]" + text[end:]
    return text


def redact_legacy_text(text: str) -> str:
    """Protect pre-intake history before truncation; never rewrite stored data."""
    if len(text) > 200_000:
        return "[Historical content omitted: exceeds the safe projection limit]"
    for start, end, _kind in reversed(secret_spans(text)):
        text = (
            text[:start] + "[REDACTED legacy credential — provide secure input to use]" + text[end:]
        )
    return text


@overload
def redact_legacy_payload(value: dict[str, Any]) -> dict[str, Any]: ...


@overload
def redact_legacy_payload(value: Any) -> Any: ...


def redact_legacy_payload(value: Any) -> Any:
    """Bounded copy for public JSON projections, including secrets in map keys."""
    remaining = 20_000

    def visit(node: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 20:
            return "[Historical content omitted: exceeds the safe projection limit]"
        if isinstance(node, str):
            return redact_legacy_text(node)
        if isinstance(node, dict):
            result = {}
            for key, item in node.items():
                if remaining < 0:
                    break
                public_key = redact_legacy_text(key) if isinstance(key, str) else key
                result[public_key] = visit(item, depth + 1)
            return result
        if isinstance(node, (list, tuple)):
            result_list = []
            for item in node:
                if remaining < 0:
                    break
                result_list.append(visit(item, depth + 1))
            return result_list
        return node

    return visit(value, 0)


@dataclass(frozen=True)
class CapturedInput:
    text: str
    references: list[dict[str, str]] = field(default_factory=list)
    requires_ghost_url: bool = False


async def capture_input(
    session: AsyncSession,
    crypto: SecretCrypto | None,
    *,
    workspace_id: UUID,
    conversation_id: UUID,
    agent_id: UUID,
    user_id: UUID,
    text: str,
    secure_inputs: list[dict[str, Any]] | None = None,
) -> CapturedInput:
    spans = secret_spans(text)
    supplied = secure_inputs or []
    if not spans and not supplied:
        return CapturedInput(text)
    if crypto is None:
        raise VariableError(
            "Secure storage is unavailable; configure the encryption key "
            "before sending credentials",
            503,
        )
    if len(spans) + len(supplied) > 20:
        raise VariableError("Send at most 20 secure values at a time", 422)
    references: list[dict[str, str]] = []
    ghost = False

    async def capture(value: str, kind: str, name: str) -> str:
        nonlocal ghost
        validate_value(value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,119}", name):
            raise VariableError("Use a short name for the secure input", 422)
        fingerprint = crypto.fingerprint(value)
        row = await session.scalar(
            select(SecureInputCapture).where(
                SecureInputCapture.workspace_id == workspace_id,
                SecureInputCapture.conversation_id == conversation_id,
                SecureInputCapture.user_id == user_id,
                SecureInputCapture.fingerprint == fingerprint,
            )
        )
        if row is None:
            capture_id = new_uuid7()
            secret = await SecretStore(session, crypto).create(
                workspace_id=workspace_id,
                name=f"secure-input/{capture_id}",
                plaintext=value,
                created_by_user_id=user_id,
            )
            secret.masked_hint = ""
            row = SecureInputCapture(
                id=capture_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                user_id=user_id,
                secret_id=secret.id,
                fingerprint=fingerprint,
                name=name,
                kind=kind,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
            session.add(row)
            await session.flush()
        elif row.variable_id is None:
            # Re-presenting the actual value renews intake, not its authority.
            row.expires_at = datetime.now(UTC) + timedelta(days=7)
        reference = {"secret_ref": str(row.id), "name": row.name, "kind": row.kind}
        if reference not in references:
            references.append(reference)
        ghost = ghost or kind == "ghost_admin_key"
        return str(row.id)

    pieces: list[str] = []
    position = 0
    for start, end, kind in spans:
        reference = await capture(
            text[start:end], kind, "ghost.admin_key" if kind == "ghost_admin_key" else "credential"
        )
        pieces.extend((text[position:start], f"[secure_input:{reference}]"))
        position = end
    pieces.append(text[position:])
    for entry in supplied:
        value = entry.get("value")
        if not isinstance(value, str):
            raise VariableError("Secure input requires a text value", 422)
        kind = "ghost_admin_key" if _GHOST.fullmatch(value) else "secret"
        reference = await capture(
            value,
            kind,
            str(
                entry.get("name")
                or ("ghost.admin_key" if kind == "ghost_admin_key" else "credential")
            ),
        )
        pieces.append(f"\n[secure_input:{reference}]")
    return CapturedInput(
        "".join(pieces), references, ghost and not supplied_ghost_admin_urls([text])
    )


GHOST_URL_REQUIREMENT = {
    "key": "ghost_admin_url",
    "label": "Ghost Admin URL",
    "value_type": "url",
    "reason": "A confirmed Ghost Admin URL is required before connecting.",
}


def merge_capture_metadata(metadata: dict[str, Any], captured: CapturedInput) -> dict[str, Any]:
    """Keep earlier setup blockers and references while adding secure intake."""
    result = dict(metadata)
    if captured.references:
        references = [r for r in result.get("secure_inputs", []) if isinstance(r, dict)]
        for reference in captured.references:
            if reference not in references:
                references.append(reference)
        result["secure_inputs"] = references[-20:]
    if captured.requires_ghost_url:
        blockers = [r for r in result.get("required_inputs", []) if isinstance(r, dict)]
        if not any(r.get("key") == "ghost_admin_url" for r in blockers):
            blockers.append(dict(GHOST_URL_REQUIREMENT))
        result["required_inputs"] = blockers
    return result
