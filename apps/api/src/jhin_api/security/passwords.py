"""Argon2id password hashing and the account password policy (plan 20.1).

Passwords are never logged and never leave this module unhashed.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from jhin_api.security.common_passwords import is_common_password

# Argon2id with library defaults, verified against the installed argon2-cffi:
# time_cost=3, memory_cost=65536 KiB (64 MiB), parallelism=4, 32-byte tag,
# 16-byte salt. That is comfortably above the OWASP 2024 floor for Argon2id
# (m=19 MiB, t=2, p=1), so the defaults are kept deliberately rather than
# by omission. ``needs_rehash`` below upgrades stored hashes for free when a
# future argon2-cffi raises them.
_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 200


class PasswordPolicyError(ValueError):
    """The candidate password does not meet the account password policy."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _hasher.verify(password_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return False


def validate_password_strength(password: str, *, email: str | None = None) -> None:
    """Raise :class:`PasswordPolicyError` when ``password`` is unacceptable.

    Intentionally simple and predictable: a 12-character minimum, a check
    against the embedded most-guessed list, and a refusal to reuse the account
    identifier. Composition rules ("one symbol, one digit") are deliberately
    absent — they push users toward predictable substitutions without adding
    real entropy (NIST SP 800-63B).
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long")
    if password.strip() == "":
        raise PasswordPolicyError("Password must not be entirely whitespace")
    if is_common_password(password):
        raise PasswordPolicyError(
            "Password is one of the most commonly guessed passwords; choose another"
        )
    if email:
        normalized = password.strip().lower()
        address = email.strip().lower()
        local_part = address.split("@", 1)[0]
        if normalized in {address, local_part} or (
            len(local_part) >= 4 and local_part in normalized
        ):
            raise PasswordPolicyError("Password must not contain your email address")
