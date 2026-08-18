"""Fail closed when production Compose renders any Phase 9 dev fixture."""

from __future__ import annotations

import json
import subprocess
from typing import Any

FORBIDDEN_MARKERS = (
    "fake-supabase-db",
    "supabase_fixture",
    "reader-pass",
    "writer-pass",
    "phase9-fixture-admin-only",
    "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
    "JHIN_CONNECTOR_ALLOWED_DB_HOSTS",
)


def assert_production_config(config: dict[str, Any]) -> None:
    """Reject dev-only services, fixtures, credentials, and allowlists."""
    services = config.get("services", {})
    if not isinstance(services, dict):
        raise ValueError("production Compose services must be a mapping")

    fake_services = sorted(name for name in services if name.startswith("fake-"))
    if fake_services:
        raise ValueError(f"production Compose contains fake service: {fake_services[0]}")

    rendered = json.dumps(config, ensure_ascii=False, sort_keys=True)
    for marker in FORBIDDEN_MARKERS:
        if marker in rendered:
            raise ValueError(f"production Compose contains dev-only marker: {marker}")


def main() -> int:
    """Render base Compose and assert that it contains no dev-only authority."""
    command = [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(command, capture_output=True, check=True, text=True)
    config = json.loads(result.stdout)
    if not isinstance(config, dict):
        raise ValueError("production Compose config must render as an object")
    assert_production_config(config)
    print("Phase 9 production Compose assertion passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
