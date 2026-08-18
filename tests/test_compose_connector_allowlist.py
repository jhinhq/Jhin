"""Rendered Compose keeps connector test origins dev-only and exact."""

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEV_CONNECTOR_ORIGINS = "http://fake-github:8080,http://fake-linear:8080"


def _render_compose(*files: str) -> dict[str, Any]:
    command = ["docker", "compose"]
    for file in files:
        command.extend(("-f", file))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered: dict[str, Any] = json.loads(completed.stdout)
    return rendered


def test_connector_origin_allowlist_is_exact_and_dev_only() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    production = _render_compose("compose.yaml")

    for service_name in ("api", "agent-worker"):
        assert (
            development["services"][service_name]["environment"][
                "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
            ]
            == DEV_CONNECTOR_ORIGINS
        )
        assert (
            "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
            not in production["services"][service_name]["environment"]
        )
