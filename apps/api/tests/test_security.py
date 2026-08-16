"""Unit tests for password hashing, session tokens, and login rate limiting."""

from jhin_api.security.passwords import hash_password, verify_password
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import hash_token, new_csrf_token, new_session_token


def test_password_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong password")


def test_password_hashes_are_salted() -> None:
    assert hash_password("same input") != hash_password("same input")


def test_verify_rejects_garbage_hash_without_raising() -> None:
    assert not verify_password("not-a-real-hash", "anything")


def test_session_tokens_are_unique_and_hashed_at_rest() -> None:
    token = new_session_token()
    assert token != new_session_token()
    digest = hash_token(token)
    assert digest != token
    assert digest == hash_token(token)  # deterministic lookup key
    assert len(digest) == 64  # sha256 hex


def test_csrf_tokens_are_random() -> None:
    assert new_csrf_token() != new_csrf_token()


def test_rate_limiter_blocks_after_max_attempts() -> None:
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=300)
    for _ in range(3):
        assert not limiter.is_blocked("a@b.c", "1.2.3.4")
        limiter.record_failure("a@b.c", "1.2.3.4")
    assert limiter.is_blocked("a@b.c", "1.2.3.4")
    # Different IP or email is tracked independently.
    assert not limiter.is_blocked("a@b.c", "5.6.7.8")
    assert not limiter.is_blocked("x@y.z", "1.2.3.4")


def test_rate_limiter_reset_on_success() -> None:
    limiter = LoginRateLimiter(max_attempts=1, window_seconds=300)
    limiter.record_failure("a@b.c", "1.2.3.4")
    assert limiter.is_blocked("a@b.c", "1.2.3.4")
    limiter.reset("a@b.c", "1.2.3.4")
    assert not limiter.is_blocked("a@b.c", "1.2.3.4")


def test_rate_limiter_email_is_normalized() -> None:
    limiter = LoginRateLimiter(max_attempts=1, window_seconds=300)
    limiter.record_failure("  A@B.C ", "1.2.3.4")
    assert limiter.is_blocked("a@b.c", "1.2.3.4")
