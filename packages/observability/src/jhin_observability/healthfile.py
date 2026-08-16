"""File-based liveness heartbeat for headless workers.

Workers touch a heartbeat file on a fixed interval; the Docker Compose
healthcheck runs the ``jhin-health-check`` console script, which exits
non-zero when the heartbeat is stale or missing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

HEALTH_FILE_ENV = "HEALTH_FILE"
MAX_AGE_ENV = "HEALTH_MAX_AGE_SECONDS"
DEFAULT_HEALTH_FILE = "/tmp/jhin-heartbeat"
DEFAULT_MAX_AGE_SECONDS = 20.0
DEFAULT_INTERVAL_SECONDS = 5.0


def heartbeat_path() -> Path:
    return Path(os.environ.get(HEALTH_FILE_ENV, DEFAULT_HEALTH_FILE))


async def run_heartbeat(interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Touch the heartbeat file forever. Run as a background task."""
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        path.touch()
        await asyncio.sleep(interval_seconds)


def clear_heartbeat() -> None:
    heartbeat_path().unlink(missing_ok=True)


def main() -> None:
    """Console-script healthcheck: exit 0 iff the heartbeat file is fresh."""
    max_age = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_SECONDS))
    try:
        age = time.time() - heartbeat_path().stat().st_mtime
    except FileNotFoundError:
        sys.exit(1)
    sys.exit(0 if age <= max_age else 1)
