"""Login lockout with progressive, time-decaying backoff (plan 20.1, 41).

Two independent buckets are tracked per attempt:

* **per account** — stops password guessing against one known email from any
  number of source addresses;
* **per source IP** — stops credential stuffing that sprays many accounts from
  one address.

Both use an exponentially *decaying* failure score rather than a fixed window,
and the block they impose is capped. That matters for a self-hosted product:
an attacker must never be able to lock a real user out permanently. Three
properties together guarantee that.

1. The failure score halves every ``half_life_seconds``, so a bucket cools off
   on its own with no operator action.
2. The block length grows exponentially but is clamped at
   ``account_max_block_seconds`` (15 minutes by default).
3. Failures arriving from an address that is *already blocked* do not add to
   the account bucket. An attacker's address trips the IP bucket quickly and
   then stops poisoning the victim's account, so the account block expires
   while the attacker stays blocked.

NOTE: deliberately in-memory and therefore per-process — replicas each keep
their own counters. Good enough while the API runs as a single container;
replace with a DB- or NATS-backed counter before scaling out.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Hard ceiling on tracked keys so an attacker cannot exhaust memory by
# guessing against endlessly many distinct emails or spoofed addresses.
MAX_TRACKED_KEYS = 20_000

# Decay is applied on every write, so N failures fired back-to-back build a
# score a shade below N. Compare against the threshold with a small tolerance
# so a burst of exactly `max_attempts` locks, rather than letting the rounding
# crumb buy an attacker one extra guess. (Failures spread out over time
# genuinely should not reach the threshold — that is what decay is for.)
_SCORE_EPSILON = 0.05


@dataclass
class _Bucket:
    score: float = 0.0
    updated_at: float = 0.0
    blocked_until: float = 0.0


@dataclass(frozen=True)
class LockoutDecision:
    """Outcome of a pre-login check."""

    blocked: bool
    scope: str = ""
    retry_after_seconds: int = 0

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.blocked


NOT_BLOCKED = LockoutDecision(blocked=False)


@dataclass
class LoginRateLimiter:
    """Progressive, decaying login lockout keyed by account and by source IP."""

    account_max_attempts: int = 10
    ip_max_attempts: int = 30
    half_life_seconds: float = 300.0
    base_block_seconds: float = 30.0
    account_max_block_seconds: float = 900.0
    ip_max_block_seconds: float = 3600.0

    _accounts: dict[str, _Bucket] = field(default_factory=dict, init=False, repr=False)
    _ips: dict[str, _Bucket] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.account_max_attempts < 1 or self.ip_max_attempts < 1:
            raise ValueError("attempt thresholds must be at least 1")
        if self.half_life_seconds <= 0:
            raise ValueError("half life must be positive")

    # -- internals -------------------------------------------------------

    @staticmethod
    def _account_key(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _ip_key(ip: str) -> str:
        return ip.strip().lower()

    def _decayed(self, bucket: _Bucket, now: float) -> float:
        """Failure score after exponential decay to ``now``."""
        elapsed = now - bucket.updated_at
        if elapsed <= 0:
            return bucket.score
        return float(bucket.score * (0.5 ** (elapsed / self.half_life_seconds)))

    def _prune(self, table: dict[str, _Bucket], now: float) -> None:
        """Drop cold buckets, then evict oldest if still over the cap."""
        stale = [
            key
            for key, bucket in table.items()
            if bucket.blocked_until <= now and self._decayed(bucket, now) < 0.01
        ]
        for key in stale:
            del table[key]
        overflow = len(table) - MAX_TRACKED_KEYS
        if overflow > 0:
            oldest = sorted(table.items(), key=lambda item: item[1].updated_at)[:overflow]
            for key, _ in oldest:
                del table[key]

    def _block_for(self, table: dict[str, _Bucket], key: str, now: float) -> LockoutDecision:
        bucket = table.get(key)
        if bucket is None or bucket.blocked_until <= now:
            return NOT_BLOCKED
        return LockoutDecision(
            blocked=True,
            scope="",
            retry_after_seconds=max(1, int(bucket.blocked_until - now + 0.999)),
        )

    def _register(
        self,
        table: dict[str, _Bucket],
        key: str,
        now: float,
        *,
        threshold: int,
        max_block: float,
    ) -> None:
        bucket = table.get(key)
        if bucket is None:
            bucket = _Bucket()
            table[key] = bucket
        bucket.score = self._decayed(bucket, now) + 1.0
        bucket.updated_at = now
        over = bucket.score - threshold
        if over >= -_SCORE_EPSILON:
            block = min(self.base_block_seconds * (2.0 ** max(over, 0.0)), max_block)
            bucket.blocked_until = max(bucket.blocked_until, now + block)
        self._prune(table, now)

    # -- public API ------------------------------------------------------

    def check(self, email: str, ip: str) -> LockoutDecision:
        """Whether this (account, address) pair may attempt a login right now."""
        now = time.monotonic()
        with self._lock:
            account = self._block_for(self._accounts, self._account_key(email), now)
            if account.blocked:
                return LockoutDecision(
                    blocked=True,
                    scope="account",
                    retry_after_seconds=account.retry_after_seconds,
                )
            source = self._block_for(self._ips, self._ip_key(ip), now)
            if source.blocked:
                return LockoutDecision(
                    blocked=True, scope="ip", retry_after_seconds=source.retry_after_seconds
                )
            return NOT_BLOCKED

    def is_blocked(self, email: str, ip: str) -> bool:
        return self.check(email, ip).blocked

    def record_failure(self, email: str, ip: str) -> None:
        now = time.monotonic()
        ip_key = self._ip_key(ip)
        with self._lock:
            # An address that is already blocked has lost the right to keep
            # driving the victim's account lockout deeper (see module docstring).
            ip_was_blocked = self._block_for(self._ips, ip_key, now).blocked
            self._register(
                self._ips,
                ip_key,
                now,
                threshold=self.ip_max_attempts,
                max_block=self.ip_max_block_seconds,
            )
            if not ip_was_blocked:
                self._register(
                    self._accounts,
                    self._account_key(email),
                    now,
                    threshold=self.account_max_attempts,
                    max_block=self.account_max_block_seconds,
                )

    def reset(self, email: str, ip: str) -> None:
        """Clear both buckets after a successful authentication."""
        with self._lock:
            self._accounts.pop(self._account_key(email), None)
            self._ips.pop(self._ip_key(ip), None)

    def tracked_keys(self) -> tuple[int, int]:
        """(accounts, addresses) currently tracked — for tests and metrics."""
        with self._lock:
            return len(self._accounts), len(self._ips)
