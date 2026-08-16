"""Opaque session tokens.

The plaintext token lives only in the user's cookie; the database stores a
SHA-256 hash, so a database leak cannot be replayed as a session (plan 20.1).
"""

import hashlib
import secrets

TOKEN_BYTES = 32


def new_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)
