"""Executable guards for the production Compose shape and test harness."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "assert_phase9_production_compose.py"
)
ROOT = Path(__file__).resolve().parents[1]


class ProductionComposeModule(Protocol):
    subprocess: ModuleType

    def assert_production_config(self, config: dict[str, Any]) -> None: ...

    def main(self) -> int: ...


def _load_production_compose_module() -> ProductionComposeModule:
    spec = importlib.util.spec_from_file_location("assert_phase9_production_compose", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(ProductionComposeModule, module)


production_compose = _load_production_compose_module()


def _safe_config() -> dict[str, Any]:
    return {
        "name": "jhin",
        "services": {
            "api": {
                "environment": {
                    "JHIN_ENV": "production",
                    "DATABASE_URL": "postgresql://jhin@postgres/jhin",
                }
            },
            "web": {"environment": {"NODE_ENV": "production"}},
        },
        "volumes": {"postgres_data": {"name": "jhin_postgres_data"}},
    }


def test_production_assertion_accepts_production_services() -> None:
    production_compose.assert_production_config(_safe_config())


@pytest.mark.parametrize("service_name", ["fake-vercel", "fake-supabase", "fake-anything"])
def test_production_assertion_rejects_fake_services(service_name: str) -> None:
    config = _safe_config()
    config["services"][service_name] = {"image": "local-only"}

    with pytest.raises(ValueError, match="fake service"):
        production_compose.assert_production_config(config)


@pytest.mark.parametrize(
    "forbidden",
    [
        "fake-supabase-db",
        "supabase_fixture",
        "reader-pass",
        "writer-pass",
        "phase9-fixture-admin-only",
        "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
        "JHIN_CONNECTOR_ALLOWED_DB_HOSTS",
    ],
)
def test_production_assertion_rejects_each_dev_only_marker(forbidden: str) -> None:
    config = _safe_config()
    config["services"]["api"]["environment"]["POISON"] = forbidden

    with pytest.raises(ValueError, match="dev-only marker"):
        production_compose.assert_production_config(config)


@pytest.mark.parametrize(
    "project",
    ["jhin", "jhin-phase9-acceptance", "jhin_9", "9-jhin"],
)
def test_compose_project_validation_accepts_safe_names(project: str) -> None:
    from tests.integration.conftest import validate_compose_project

    assert validate_compose_project(project) == project


@pytest.mark.parametrize(
    "project",
    ["", "Jhin", "-jhin", "_jhin", "jhin/other", "jhin other", "jhin.$(id)"],
)
def test_compose_project_validation_rejects_unsafe_names(project: str) -> None:
    from tests.integration.conftest import validate_compose_project

    with pytest.raises(ValueError, match="JHIN_TEST_COMPOSE_PROJECT"):
        validate_compose_project(project)


def test_integration_compose_uses_literal_validated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import conftest as harness

    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="api\n", stderr="")

    monkeypatch.setenv("JHIN_TEST_COMPOSE_PROJECT", "jhin-phase9-acceptance")
    monkeypatch.setattr(subprocess, "run", fake_run)

    harness.compose("ps", timeout=7.0)

    assert observed == {
        "command": [
            "docker",
            "compose",
            "-p",
            "jhin-phase9-acceptance",
            "-f",
            "compose.yaml",
            "-f",
            "compose.dev.yaml",
            "ps",
        ],
        "kwargs": {
            "cwd": harness.REPO_ROOT,
            "capture_output": True,
            "text": True,
            "timeout": 7.0,
            "check": True,
        },
    }


def test_production_assertion_cli_renders_only_base_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_safe_config()), stderr="")

    monkeypatch.setattr(production_compose.subprocess, "run", fake_run)

    assert production_compose.main() == 0
    assert observed == {
        "command": [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        "kwargs": {"capture_output": True, "check": True, "text": True},
    }


def test_production_nats_frame_limit_carries_an_exact_cap_webhook_envelope() -> None:
    completed = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered: dict[str, Any] = json.loads(completed.stdout)
    nats = rendered["services"]["nats"]
    assert nats["command"] == ["--config", "/etc/nats/nats.conf"]
    config_mount = next(
        volume for volume in nats["volumes"] if volume["target"] == "/etc/nats/nats.conf"
    )
    assert config_mount["type"] == "bind"
    assert config_mount["source"] == str((ROOT / "config/nats.conf").resolve())
    assert config_mount["read_only"] is True
    nats_config = (ROOT / "config/nats.conf").read_text(encoding="utf-8")
    assert "max_payload: 2097152" in nats_config


def test_production_temporal_binds_all_interfaces_across_isolated_networks() -> None:
    completed = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered: dict[str, Any] = json.loads(completed.stdout)
    temporal = rendered["services"]["temporal"]

    assert set(temporal["networks"]) == {"control", "data"}
    assert temporal["environment"]["BIND_ON_IP"] == "0.0.0.0"
