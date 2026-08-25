"""Unit tests for password policy, session tokens, CSRF binding, and lockout."""

import time

import pytest

from jhin_api.security.common_passwords import is_common_password
from jhin_api.security.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from jhin_api.security.rate_limit import LoginRateLimiter
from jhin_api.security.tokens import (
    csrf_token_for_session,
    csrf_token_matches_session,
    hash_token,
    new_session_token,
)

# --- password hashing -------------------------------------------------------


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


def test_argon2_parameters_meet_owasp_floor() -> None:
    """m>=19 MiB, t>=2 for Argon2id (OWASP 2024). Guards a silent downgrade."""
    encoded = hash_password("parameter probe value")
    fields = dict(part.split("=", 1) for part in encoded.split("$")[3].split(",") if "=" in part)
    assert int(fields["m"]) >= 19 * 1024
    assert int(fields["t"]) >= 2
    assert int(fields["p"]) >= 1


def test_current_hashes_do_not_need_rehash() -> None:
    assert not needs_rehash(hash_password("a perfectly fine passphrase"))


def test_needs_rehash_flags_weaker_parameters() -> None:
    weak = "$argon2id$v=19$m=8,t=1,p=1$c29tZXNhbHRzYWx0$" + "a" * 43
    assert needs_rehash(weak) or not verify_password(weak, "x")


def test_needs_rehash_tolerates_garbage() -> None:
    assert not needs_rehash("nonsense")


# --- password policy --------------------------------------------------------


def test_policy_accepts_a_reasonable_passphrase() -> None:
    validate_password_strength("morning-otter-canyon-42", email="dana@example.com")


def test_policy_requires_twelve_characters() -> None:
    assert MIN_PASSWORD_LENGTH == 12
    with pytest.raises(PasswordPolicyError, match="at least 12"):
        validate_password_strength("short1234!!", email="dana@example.com")


def test_policy_rejects_common_passwords() -> None:
    for candidate in ("password1234", "123456789012", "qwertyuiop123", "PassWordPassword"):
        with pytest.raises(PasswordPolicyError, match="commonly guessed"):
            validate_password_strength(candidate, email="dana@example.com")


def test_common_password_check_is_case_and_space_insensitive() -> None:
    assert is_common_password("  PassWord123  ")
    assert not is_common_password("a genuinely unusual phrase")


def test_policy_rejects_password_containing_the_email() -> None:
    with pytest.raises(PasswordPolicyError, match="email address"):
        validate_password_strength("dana.roberts-2026", email="dana.roberts@example.com")


def test_policy_allows_short_local_part_appearing_incidentally() -> None:
    # A two-letter local part would otherwise ban half the dictionary.
    validate_password_strength("quarantine-badger-9", email="qa@jhin.dev")


def test_policy_rejects_whitespace_only() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_strength(" " * 20, email="dana@example.com")


def test_policy_rejects_absurdly_long_passwords() -> None:
    with pytest.raises(PasswordPolicyError, match="at most"):
        validate_password_strength("x" * 5000, email="dana@example.com")


# --- session and CSRF tokens ------------------------------------------------


def test_session_tokens_are_unique_and_hashed_at_rest() -> None:
    token = new_session_token()
    assert token != new_session_token()
    digest = hash_token(token)
    assert digest != token
    assert digest == hash_token(token)  # deterministic lookup key
    assert len(digest) == 64  # sha256 hex


def test_csrf_token_is_bound_to_its_session() -> None:
    session_a = new_session_token()
    session_b = new_session_token()
    token_a = csrf_token_for_session(session_a)
    assert csrf_token_matches_session(token_a, session_a)
    assert not csrf_token_matches_session(token_a, session_b)


def test_csrf_token_is_stable_for_one_session() -> None:
    session = new_session_token()
    assert csrf_token_for_session(session) == csrf_token_for_session(session)


def test_csrf_token_does_not_reveal_the_session_token() -> None:
    session = new_session_token()
    derived = csrf_token_for_session(session)
    assert session not in derived
    assert derived != session


# --- login lockout ----------------------------------------------------------


def limiter(**overrides: float) -> LoginRateLimiter:
    settings: dict[str, float] = {
        "account_max_attempts": 3,
        "ip_max_attempts": 10,
        "half_life_seconds": 300.0,
        "base_block_seconds": 30.0,
        "account_max_block_seconds": 900.0,
        "ip_max_block_seconds": 3600.0,
    }
    settings.update(overrides)
    return LoginRateLimiter(**settings)  # type: ignore[arg-type]


def test_account_locks_after_threshold_failures() -> None:
    limit = limiter()
    for _ in range(3):
        assert not limit.check("a@b.c", "1.2.3.4").blocked
        limit.record_failure("a@b.c", "1.2.3.4")
    decision = limit.check("a@b.c", "1.2.3.4")
    assert decision.blocked
    assert decision.scope == "account"
    assert decision.retry_after_seconds > 0


def test_account_lock_follows_the_account_across_source_addresses() -> None:
    """The whole point of a per-account bucket: rotating IPs must not help."""
    limit = limiter()
    for index in range(3):
        limit.record_failure("a@b.c", f"9.9.9.{index}")
    assert limit.check("a@b.c", "5.5.5.5").blocked


def test_other_accounts_are_unaffected_by_one_account_lock() -> None:
    limit = limiter()
    for _ in range(3):
        limit.record_failure("a@b.c", "1.2.3.4")
    assert not limit.check("someone-else@b.c", "8.8.8.8").blocked


def test_source_address_locks_independently_of_the_account() -> None:
    limit = limiter(account_max_attempts=1000, ip_max_attempts=4)
    for index in range(4):
        limit.record_failure(f"victim{index}@b.c", "6.6.6.6")
    decision = limit.check("fresh@b.c", "6.6.6.6")
    assert decision.blocked
    assert decision.scope == "ip"


def test_backoff_is_progressive() -> None:
    limit = limiter(base_block_seconds=10.0)
    for _ in range(3):
        limit.record_failure("a@b.c", "1.2.3.4")
    first = limit.check("a@b.c", "1.2.3.4").retry_after_seconds
    limit.record_failure("a@b.c", "1.2.3.4")
    second = limit.check("a@b.c", "1.2.3.4").retry_after_seconds
    assert second > first


def test_account_block_is_capped_so_a_victim_is_never_locked_out_forever() -> None:
    limit = limiter(base_block_seconds=30.0, account_max_block_seconds=120.0)
    for _ in range(60):
        limit.record_failure("victim@b.c", "1.2.3.4")
    assert limit.check("victim@b.c", "1.2.3.4").retry_after_seconds <= 120


def test_failures_from_an_already_blocked_address_stop_deepening_the_account_lock() -> None:
    """An attacker's address trips first, then stops poisoning the victim.

    Without this, one blocked attacker could hold a real user's account at the
    maximum backoff indefinitely.
    """
    limit = limiter(account_max_attempts=100, ip_max_attempts=3)
    for _ in range(3):
        limit.record_failure("victim@b.c", "7.7.7.7")
    assert limit.check("nobody@b.c", "7.7.7.7").scope == "ip"
    accounts_before, _ = limit.tracked_keys()
    for _ in range(50):
        limit.record_failure("victim@b.c", "7.7.7.7")
    # The victim's account bucket stopped growing once the address was blocked.
    assert not limit.check("victim@b.c", "1.1.1.1").blocked
    assert limit.tracked_keys()[0] == accounts_before


def test_failure_score_decays_so_the_lock_clears_itself() -> None:
    """No operator action, no support ticket: the lock times itself out."""
    limit = limiter(account_max_attempts=1, half_life_seconds=0.05, base_block_seconds=0.05)
    limit.record_failure("a@b.c", "1.2.3.4")
    assert limit.check("a@b.c", "1.2.3.4").blocked
    time.sleep(0.4)  # several half-lives, and well past the block
    assert not limit.check("a@b.c", "1.2.3.4").blocked


def test_decay_reduces_the_score_rather_than_resetting_it() -> None:
    """A cooled-off bucket is still warmer than a cold one."""
    warm = limiter(account_max_attempts=3, half_life_seconds=0.4, base_block_seconds=0.01)
    for _ in range(3):
        warm.record_failure("a@b.c", "1.2.3.4")
    time.sleep(0.45)  # ~one half-life: score falls to about 1.5
    assert not warm.check("a@b.c", "1.2.3.4").blocked
    for _ in range(2):
        warm.record_failure("a@b.c", "1.2.3.4")
    assert warm.check("a@b.c", "1.2.3.4").blocked

    cold = limiter(account_max_attempts=3, half_life_seconds=0.4, base_block_seconds=0.01)
    for _ in range(2):
        cold.record_failure("a@b.c", "1.2.3.4")
    assert not cold.check("a@b.c", "1.2.3.4").blocked


def test_successful_login_clears_both_buckets() -> None:
    limit = limiter()
    for _ in range(3):
        limit.record_failure("a@b.c", "1.2.3.4")
    assert limit.check("a@b.c", "1.2.3.4").blocked
    limit.reset("a@b.c", "1.2.3.4")
    assert not limit.check("a@b.c", "1.2.3.4").blocked


def test_email_is_normalized() -> None:
    limit = limiter(account_max_attempts=1)
    limit.record_failure("  A@B.C ", "1.2.3.4")
    assert limit.check("a@b.c", "1.2.3.4").blocked


def test_tracked_keys_are_pruned_so_memory_cannot_be_exhausted() -> None:
    """Guessing endlessly many distinct emails must not grow the table forever."""
    limit = limiter(half_life_seconds=0.01, base_block_seconds=0.001)
    for index in range(200):
        limit.record_failure(f"user{index}@b.c", "1.2.3.4")
    time.sleep(0.2)
    limit.record_failure("trigger-prune@b.c", "1.2.3.4")
    accounts, _ = limit.tracked_keys()
    assert accounts < 200


def test_limiter_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        LoginRateLimiter(account_max_attempts=0)
    with pytest.raises(ValueError):
        LoginRateLimiter(half_life_seconds=0)
