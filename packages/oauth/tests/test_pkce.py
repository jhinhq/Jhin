"""PKCE verifiers, challenges, and the opaque state handle.

The RFC 7636 Appendix B vector is checked against a reference computed here,
and the implementation is then checked against that reference — so neither
side gets to define what "correct" means on its own.

The security property under test is negative: there is no way, through any
public function in :mod:`jhin_oauth`, to produce a ``plain`` challenge. A
downgrade has to be unreachable, not merely discouraged.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import string
from dataclasses import FrozenInstanceError

import pytest

from jhin_oauth.pkce import generate_pkce, generate_state, state_hash
from jhin_oauth.types import PkcePair

# RFC 7636 §4.1: the verifier alphabet is the unreserved characters only.
UNRESERVED = set(string.ascii_letters + string.digits + "-._~")
STATE_ALPHABET = set(string.ascii_letters + string.digits + "-_")

# RFC 7636 Appendix B, verbatim.
RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _reference_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_the_reference_matches_the_rfc_7636_test_vector() -> None:
    assert _reference_challenge(RFC_7636_VERIFIER) == RFC_7636_CHALLENGE


def test_verifier_is_43_unreserved_characters() -> None:
    """32 random bytes base64url-encoded, the RFC's own recommendation."""
    pair = generate_pkce()
    assert len(pair.verifier) == 43
    assert set(pair.verifier) <= UNRESERVED


def test_generated_challenge_is_the_s256_of_its_own_verifier() -> None:
    pair = generate_pkce()
    assert pair.challenge == _reference_challenge(pair.verifier)
    assert "=" not in pair.challenge


def test_method_is_always_s256() -> None:
    assert generate_pkce().method == "S256"


def test_pkce_pairs_are_frozen_so_a_method_cannot_be_downgraded_in_place() -> None:
    pair = generate_pkce()
    with pytest.raises(FrozenInstanceError):
        pair.method = "plain"  # type: ignore[misc]


def test_no_public_function_produces_a_plain_challenge() -> None:
    """``generate_pkce`` takes no argument that could weaken the method."""
    assert inspect.signature(generate_pkce).parameters == {}
    assert PkcePair.__dataclass_fields__["method"].default == "S256"


def test_every_pair_is_fresh() -> None:
    verifiers = {generate_pkce().verifier for _ in range(50)}
    assert len(verifiers) == 50


def test_state_is_fresh_and_url_safe() -> None:
    states = {generate_state() for _ in range(50)}
    assert len(states) == 50
    for value in states:
        assert set(value) <= STATE_ALPHABET
        # token_urlsafe(32) is 43 characters; the callback validator accepts
        # 1..256 characters of exactly this alphabet.
        assert 40 <= len(value) <= 256


def test_state_hash_is_stable_lowercase_hex_sha256() -> None:
    digest = state_hash("a-known-state")
    assert digest == state_hash("a-known-state")
    assert digest == hashlib.sha256(b"a-known-state").hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()


def test_state_hash_separates_values_that_differ_only_slightly() -> None:
    assert state_hash("a-known-statf") != state_hash("a-known-state")
    assert state_hash("AbC") != state_hash("abc")
