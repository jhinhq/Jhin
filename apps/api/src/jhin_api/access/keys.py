"""API key format, hashing, and authentication (docs/architecture/api-keys.md).

Wire format::

    Authorization: Bearer jhin_<prefix>_<secret>

``prefix`` is a public 8-character identifier stored in clear so a key row can
be found with one indexed lookup; ``secret`` is 32 random bytes, URL-safe
base64. Only ``sha256(secret)`` is persisted.

Why a plain SHA-256 and not Argon2 like passwords: the secret is 256 bits of
CSPRNG output, so there is no dictionary to search and no useful work factor —
guessing is infeasible regardless of hash speed. A slow KDF would only add
per-request latency to every API call. This is the same reasoning (and the
same helper) behind session-token storage in ``security/tokens.py``.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.security.tokens import hash_token
from jhin_db.models import ApiKey, User
from jhin_domain import UserStatus, WorkspaceRole, effective_scopes

KEY_LABEL = "jhin"
PREFIX_BYTES = 4  # 8 hex characters
SECRET_BYTES = 32


@dataclass(frozen=True)
class GeneratedKey:
    """The one moment the plaintext exists; the caller must not persist it."""

    plaintext: str
    prefix: str
    key_hash: str


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """An authenticated API key, resolved to what it may actually do."""

    id: UUID
    workspace_id: UUID
    name: str
    prefix: str
    role_ceiling: WorkspaceRole
    # Already intersected with the ceiling: this is the effective set.
    scopes: frozenset[str]


class ApiKeyAuthError(Exception):
    """Authentication failed. ``rate_limited`` marks a countable bad attempt."""

    def __init__(self, message: str, *, rate_limited: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.rate_limited = rate_limited


def generate_key() -> GeneratedKey:
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return GeneratedKey(
        plaintext=f"{KEY_LABEL}_{prefix}_{secret}",
        prefix=prefix,
        key_hash=hash_token(secret),
    )


def parse_key(raw: str) -> tuple[str, str] | None:
    """Split ``jhin_<prefix>_<secret>`` into its parts, or None if malformed.

    ``maxsplit=2`` matters: the URL-safe secret may itself contain ``_``.
    """
    parts = raw.strip().split("_", 2)
    if len(parts) != 3:
        return None
    label, prefix, secret = parts
    if label != KEY_LABEL or not prefix or not secret:
        return None
    if len(prefix) != PREFIX_BYTES * 2 or not all(c in "0123456789abcdef" for c in prefix):
        return None
    return prefix, secret


def bearer_token(authorization: str | None) -> str | None:
    """Extract a Jhin API key from an Authorization header, if present."""
    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    candidate = value.strip()
    return candidate if candidate.startswith(f"{KEY_LABEL}_") else None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def authenticate(db: AsyncSession, raw: str) -> tuple[ApiKeyPrincipal, User]:
    """Resolve a plaintext key to its principal, or raise :class:`ApiKeyAuthError`.

    Every failure carries the same outward-facing wording so a caller cannot
    tell an unknown prefix from a wrong secret from a revoked key.
    """
    parsed = parse_key(raw)
    if parsed is None:
        raise ApiKeyAuthError("Invalid or expired API key")
    prefix, secret = parsed

    record = await db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if record is None:
        raise ApiKeyAuthError("Invalid or expired API key")
    if not hmac.compare_digest(record.key_hash, hash_token(secret)):
        raise ApiKeyAuthError("Invalid or expired API key")

    now = datetime.now(UTC)
    if record.revoked_at is not None:
        raise ApiKeyAuthError("Invalid or expired API key", rate_limited=False)
    if record.expires_at is not None and _as_utc(record.expires_at) <= now:
        raise ApiKeyAuthError("Invalid or expired API key", rate_limited=False)

    if record.created_by_user_id is None:
        raise ApiKeyAuthError("Invalid or expired API key", rate_limited=False)
    user = await db.get(User, record.created_by_user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise ApiKeyAuthError("Invalid or expired API key", rate_limited=False)

    ceiling = WorkspaceRole(record.role_ceiling)
    principal = ApiKeyPrincipal(
        id=record.id,
        workspace_id=record.workspace_id,
        name=record.name,
        prefix=record.prefix,
        role_ceiling=ceiling,
        scopes=effective_scopes(record.scopes_json, ceiling),
    )
    record.last_used_at = now
    await db.commit()
    return principal, user
