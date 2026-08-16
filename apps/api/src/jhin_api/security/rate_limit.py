"""Login rate limiting (plan 20.1, 41).

NOTE: deliberately a simple in-memory fixed-window counter, which is
per-process only — replicas each keep their own window. Good enough while the
API runs as a single container; replace with a DB- or NATS-backed counter
before scaling out.
"""

from __future__ import annotations

import threading
import time


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _key(self, email: str, ip: str) -> str:
        return f"{email.strip().lower()}|{ip}"

    def is_blocked(self, email: str, ip: str) -> bool:
        with self._lock:
            entry = self._attempts.get(self._key(email, ip))
            if entry is None:
                return False
            window_start, count = entry
            if time.monotonic() - window_start > self._window_seconds:
                return False
            return count >= self._max_attempts

    def record_failure(self, email: str, ip: str) -> None:
        key = self._key(email, ip)
        now = time.monotonic()
        with self._lock:
            entry = self._attempts.get(key)
            if entry is None or now - entry[0] > self._window_seconds:
                self._attempts[key] = (now, 1)
            else:
                self._attempts[key] = (entry[0], entry[1] + 1)

    def reset(self, email: str, ip: str) -> None:
        self._attempts.pop(self._key(email, ip), None)
