"""PKCE and the opaque state handle.

RFC 7636 with ``S256`` and nothing else. There is no parameter anywhere in
this module that produces a ``plain`` challenge, and no way to construct one
through :mod:`jhin_oauth` — a downgrade has to be a code change, reviewed as
one.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from jhin_oauth.types import PkcePair
from jhin_secrets.redaction import get_redactor

VERIFIER_BYTES = 32
"""32 random bytes render as a 43-character verifier, which is what RFC 7636
§4.1 recommends and the shortest length that carries 256 bits."""

STATE_BYTES = 32


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code_challenge(verifier: str) -> str:
    """``base64url(sha256(ascii(verifier)))`` — RFC 7636 §4.2."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_pkce() -> PkcePair:
    """A fresh verifier and its ``S256`` challenge.

    The verifier is registered with the process redactor before it is returned.
    A verifier plus an intercepted code is a token, which makes it credential
    material, and credential material is registered at the moment of first
    possession rather than when something happens to store it.
    """
    verifier = _b64url(secrets.token_bytes(VERIFIER_BYTES))
    get_redactor().register(verifier)
    return PkcePair(verifier=verifier, challenge=_code_challenge(verifier))


def generate_state() -> str:
    """A fresh opaque handle for one pending authorization.

    The raw value goes into the authorization URL and nowhere else; what Jhin
    persists is :func:`state_hash` of it.
    """
    return secrets.token_urlsafe(STATE_BYTES)


def state_hash(state: str) -> str:
    """Lowercase hex SHA-256 — the lookup key for a pending authorization.

    Hashing means a database read cannot hand an attacker a usable ``state``,
    and it makes the column safe to index and log.
    """
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


__all__ = ["STATE_BYTES", "VERIFIER_BYTES", "generate_pkce", "generate_state", "state_hash"]
