"""A small embedded list of the most-guessed passwords.

Deliberately tiny and offline: a self-hosted install has no network budget for
a breach-corpus lookup, and the goal is only to stop the handful of passwords
that credential-stuffing lists try first. Entries are stored lowercase and are
compared case-insensitively after stripping surrounding whitespace.

Sources: the perennial top entries of the SecLists / "worst passwords" lists,
plus the obvious Jhin-flavoured guesses an attacker would try against this
product specifically.
"""

from __future__ import annotations

COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        # Classic top-100 offenders (padded variants included because the
        # 12-character minimum otherwise makes them "long enough").
        "123456",
        "123456789",
        "12345678",
        "1234567890",
        "123456789012",
        "12345678901234",
        "111111111111",
        "000000000000",
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "passw0rd123",
        "p@ssw0rd123",
        "p@ssword123",
        "passwordpassword",
        "qwertyuiop",
        "qwerty123456",
        "qwertyuiop123",
        "1qaz2wsx3edc",
        "zaq12wsxcde3",
        "asdfghjkl123",
        "iloveyou1234",
        "letmein12345",
        "letmeinplease",
        "welcome123456",
        "welcome123",
        "admin1234567",
        "administrator",
        "adminadmin123",
        "root12345678",
        "changeme1234",
        "changemeplease",
        "trustno1234567",
        "superman1234",
        "monkey123456",
        "dragon123456",
        "football1234",
        "baseball1234",
        "sunshine1234",
        "princess1234",
        "starwars1234",
        "whatever1234",
        "computer1234",
        "abcd1234abcd",
        "abcdefghijkl",
        "aaaaaaaaaaaa",
        "secret123456",
        "temporary123",
        "temppassword",
        "defaultpassword",
        "correcthorsebatterystaple",
        # Product-shaped guesses: an attacker's first tries against Jhin.
        "jhinjhinjhin",
        "jhinpassword",
        "jhin12345678",
        "jhin-password",
        "jhinadmin123",
        "selfhosted123",
        "workspace123",
        "agentagent12",
    }
)


def is_common_password(password: str) -> bool:
    """True when the candidate is on the embedded most-guessed list."""
    return password.strip().lower() in COMMON_PASSWORDS
