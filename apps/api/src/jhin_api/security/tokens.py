"""Opaque session tokens and the session-bound CSRF token derived from them.

The plaintext session token lives only in the user's cookie; the database
stores a SHA-256 hash, so a database leak cannot be replayed as a session
(plan 20.1).

The CSRF token is *derived* from the session token with an HMAC keyed by the
session token itself. That makes the double-submit cookie session-bound with
no server-side state and no extra configuration: an attacker who can plant
cookies in the victim's browser (a hostile sibling subdomain, or a network
position on a plaintext deployment) cannot compute a CSRF token that matches
the victim's session, and reading the JavaScript-readable CSRF cookie reveals
nothing about the HttpOnly session token because SHA-256 is one-way.
"""

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32

# Domain-separation label; bump the version suffix if the derivation changes.
_CSRF_LABEL = b"jhin-csrf-binding-v1"


def new_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for_session(session_token: str) -> str:
    """Deterministic CSRF token bound to one session token."""
    return hmac.new(session_token.encode(), _CSRF_LABEL, hashlib.sha256).hexdigest()


def csrf_token_matches_session(csrf_token: str, session_token: str) -> bool:
    return hmac.compare_digest(csrf_token, csrf_token_for_session(session_token))
