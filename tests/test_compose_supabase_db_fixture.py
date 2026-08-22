"""Rendered Compose contract for the isolated Phase 9 PostgreSQL fixture."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HOST = "fake-supabase-db:5432"
FIXTURE_MARKERS = (
    "fake-supabase-db",
    "fake_supabase_db_data",
    "supabase_fixture",
    "phase9-fixture-admin-only",
    "reader-pass",
    "writer-pass",
    "FAKE_SUPABASE_DB_DEV_PORT",
)


def _render_compose(*files: str, project_name: str = "jhin") -> dict[str, Any]:
    command = ["docker", "compose", "-p", project_name]
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


def test_supabase_database_fixture_is_absent_from_production_compose() -> None:
    production = _render_compose("compose.yaml")
    serialized = json.dumps(production, sort_keys=True)

    assert "JHIN_CONNECTOR_ALLOWED_DB_HOSTS" not in serialized
    for marker in FIXTURE_MARKERS:
        assert marker not in serialized


def test_development_compose_defines_an_isolated_sentinel_ready_fixture() -> None:
    development = _render_compose(
        "compose.yaml",
        "compose.dev.yaml",
        project_name="jhin-fixture-contract-a",
    )
    service = development["services"]["fake-supabase-db"]

    assert service["image"] == "postgres:17-alpine"
    assert service["environment"] == {
        "POSTGRES_DB": "supabase_fixture",
        "POSTGRES_PASSWORD": "phase9-fixture-admin-only",
        "POSTGRES_USER": "postgres",
    }
    assert service["networks"] == {"data": None}
    assert service["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "55433",
            "target": 5432,
        }
    ]

    volumes = service["volumes"]
    assert {
        "type": "volume",
        "source": "fake_supabase_db_data",
        "target": "/var/lib/postgresql/data",
        "volume": {"nocopy": True},
    } in volumes
    init_mount = next(
        volume
        for volume in volumes
        if volume["target"] == "/docker-entrypoint-initdb.d/00-jhin-fixture.sql"
    )
    assert init_mount["type"] == "bind"
    assert init_mount["source"] == str((ROOT / "tests/fixtures/supabase/init.sql").resolve())
    assert init_mount["read_only"] is True

    first_volume_name = development["volumes"]["fake_supabase_db_data"]["name"]
    second_project = _render_compose(
        "compose.yaml",
        "compose.dev.yaml",
        project_name="jhin-fixture-contract-b",
    )
    second_volume_name = second_project["volumes"]["fake_supabase_db_data"]["name"]
    assert first_volume_name == "jhin-fixture-contract-a_fake_supabase_db_data"
    assert second_volume_name == "jhin-fixture-contract-b_fake_supabase_db_data"
    assert first_volume_name != second_volume_name
    health_command = " ".join(service["healthcheck"]["test"])
    assert "psql" in health_command
    assert "supabase_fixture" in health_command
    assert "public.fixture_ready" in health_command
    assert "pg_isready" not in health_command


def test_stateful_named_volumes_never_copy_image_metadata() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")

    expected = {
        ("postgres", "postgres_data", "/var/lib/postgresql/data"),
        ("nats", "nats_data", "/data"),
        ("fake-supabase-db", "fake_supabase_db_data", "/var/lib/postgresql/data"),
    }
    observed = {
        (service_name, mount["source"], mount["target"]): mount
        for service_name, service in development["services"].items()
        for mount in service.get("volumes", [])
        if mount.get("type") == "volume"
    }
    assert set(observed) == expected
    assert all(mount.get("volume") == {"nocopy": True} for mount in observed.values())


def test_only_database_callers_receive_the_dev_fixture_allowlist() -> None:
    development = _render_compose("compose.yaml", "compose.dev.yaml")

    recipients = {
        service_name
        for service_name, service in development["services"].items()
        if "JHIN_CONNECTOR_ALLOWED_DB_HOSTS" in service.get("environment", {})
    }
    assert recipients == {"api", "tool-worker"}
    for service_name in recipients:
        assert (
            development["services"][service_name]["environment"]["JHIN_CONNECTOR_ALLOWED_DB_HOSTS"]
            == FIXTURE_HOST
        )


def test_environment_example_documents_only_commented_fixture_connections() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active_keys = {
        line.split("=", 1)[0] for line in lines if line and not line.startswith("#") and "=" in line
    }

    assert "FAKE_SUPABASE_DB_DEV_PORT" not in active_keys
    assert "JHIN_PHASE9_DB_READER_DSN" not in active_keys
    assert "JHIN_PHASE9_DB_WRITER_DSN" not in active_keys
    assert "JHIN_PHASE9_DB_ADMIN_DSN" not in active_keys
    assert "# FAKE_SUPABASE_DB_DEV_PORT=55433" in lines
    assert (
        "# JHIN_PHASE9_DB_READER_DSN=postgresql://jhin_reader:reader-pass@"
        "127.0.0.1:55433/supabase_fixture"
    ) in lines
    assert (
        "# JHIN_PHASE9_DB_WRITER_DSN=postgresql://jhin_writer:writer-pass@"
        "127.0.0.1:55433/supabase_fixture"
    ) in lines
    assert (
        "# JHIN_PHASE9_DB_ADMIN_DSN=postgresql://postgres:phase9-fixture-admin-only@"
        "127.0.0.1:55433/supabase_fixture"
    ) in lines
