"""Rendered Compose contract for Phase 9's dev-only HTTP provider fakes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEV_HTTP_ORIGINS = (
    "http://fake-github:8080,http://fake-linear:8080,"
    "http://fake-vercel:8080,http://fake-supabase:8080"
)
FAKE_SERVICES = {
    "fake-vercel": {
        "module": "jhin_connectors.testing.fake_vercel",
        "port": "8094",
    },
    "fake-supabase": {
        "module": "jhin_connectors.testing.fake_supabase",
        "port": "8095",
    },
}


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


def test_phase9_http_fakes_are_dev_only_healthy_and_loopback_bound() -> None:
    """Catches accidental exposure, missing health probes, or wrong fake entrypoints."""
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    production = _render_compose("compose.yaml")

    for service_name, expected in FAKE_SERVICES.items():
        assert service_name not in production["services"]

        service = development["services"][service_name]
        assert service["build"] == {
            "context": str(ROOT),
            "dockerfile": "docker/python.Dockerfile",
            "args": {"SERVICE_PACKAGE": "jhin-agent-worker"},
        }
        assert service["command"] == ["python", "-m", expected["module"]]
        assert service["networks"] == {"data": None}
        assert service["ports"] == [
            {
                "host_ip": "127.0.0.1",
                "mode": "ingress",
                "protocol": "tcp",
                "published": expected["port"],
                "target": 8080,
            }
        ]
        healthcheck = service["healthcheck"]
        assert healthcheck["test"] == [
            "CMD",
            "python",
            "-c",
            "import urllib.request; "
            "urllib.request.urlopen('http://localhost:8080/_state', timeout=3)",
        ]


def test_phase9_http_origins_extend_existing_dev_allowlist_only() -> None:
    """Catches worker/API drift and leaking dev providers into production."""
    development = _render_compose("compose.yaml", "compose.dev.yaml")
    production = _render_compose("compose.yaml")

    for service_name in ("api", "agent-worker"):
        assert (
            development["services"][service_name]["environment"][
                "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
            ]
            == DEV_HTTP_ORIGINS
        )
        assert (
            "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
            not in production["services"][service_name]["environment"]
        )


def test_environment_example_documents_only_commented_http_fake_ports() -> None:
    """Catches publishing real credentials or leaving dev fake ports undiscoverable."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active_keys = {
        line.split("=", 1)[0] for line in lines if line and not line.startswith("#") and "=" in line
    }

    assert "FAKE_VERCEL_DEV_PORT" not in active_keys
    assert "FAKE_SUPABASE_DEV_PORT" not in active_keys
    assert "# FAKE_VERCEL_DEV_PORT=8094" in lines
    assert "# FAKE_SUPABASE_DEV_PORT=8095" in lines
