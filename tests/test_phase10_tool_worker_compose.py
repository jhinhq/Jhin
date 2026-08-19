"""Executable contract for the Phase 10 tool-worker Compose authority boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "assert_phase10_tool_worker_compose.py"
ROOTLESS_SOCKET = "/run/user/10001/docker.sock"
ROOTLESS_GID_CANARY = "phase10-rootless-gid-canary-73191"
ROOTFUL_TEST_GID = 10001


class ComposeContractModule(Protocol):
    ROOT: Path
    subprocess: ModuleType

    @staticmethod
    def compose_files(mode: str, *, dev: bool = False) -> tuple[str, ...]: ...

    @staticmethod
    def render_compose(
        mode: str,
        *,
        dev: bool = False,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    @staticmethod
    def assert_rendered_contract(
        config: dict[str, Any],
        *,
        mode: str,
        dev: bool,
        expected_rootful_gid: int | None = None,
        expected_socket_source: str | None = None,
    ) -> None: ...

    @staticmethod
    def validate_rootful_socket(
        socket_path: str,
        configured_gid: str,
        *,
        _lstat: Any = ...,
    ) -> tuple[str, int]: ...

    @staticmethod
    def main(argv: list[str] | None = None) -> int: ...


def _load_contract() -> ComposeContractModule:
    spec = importlib.util.spec_from_file_location("phase10_compose_contract", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(ComposeContractModule, module)


def _raw_render(*files: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COMPOSE_", "PHASE10_", "SANDBOX_DOCKER_")) and key != "APP_ENV"
    }
    process_env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    process_env.update(env or {})
    command = ["docker", "compose"]
    for filename in files:
        command.extend(("-f", filename))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=process_env,
        check=True,
        text=True,
        capture_output=True,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.mark.parametrize("mode", ["rootful", "rootless"])
@pytest.mark.parametrize("dev", [False, True])
def test_shared_contract_accepts_every_supported_render(
    mode: str,
    dev: bool,
    tmp_path: Path,
) -> None:
    contract = _load_contract()
    app_env = "test" if dev else "production"
    if mode == "rootless":
        env = {
            "APP_ENV": app_env,
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_DOCKER_GID": ROOTLESS_GID_CANARY,
        }
        rendered = contract.render_compose(mode, dev=dev, env=env)
        contract.assert_rendered_contract(rendered, mode=mode, dev=dev)
        assert ROOTLESS_GID_CANARY not in json.dumps(rendered, sort_keys=True)
        return

    with tempfile.TemporaryDirectory(prefix="p10-", dir="/tmp") as short_directory:
        socket_path = Path(short_directory) / "docker.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            gid = ROOTFUL_TEST_GID
            rendered = contract.render_compose(
                mode,
                dev=dev,
                env={
                    "APP_ENV": app_env,
                    "SANDBOX_DOCKER_GID": str(gid),
                    "SANDBOX_DOCKER_SOCKET_HOST": str(socket_path),
                },
            )
    contract.assert_rendered_contract(
        rendered,
        mode=mode,
        dev=dev,
        expected_rootful_gid=gid,
        expected_socket_source=str(socket_path),
    )


def test_dev_defaults_to_dev_but_explicit_test_app_env_wins() -> None:
    contract = _load_contract()
    defaulted = contract.render_compose(
        "rootless",
        dev=True,
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    explicit = contract.render_compose(
        "rootless",
        dev=True,
        env={
            "APP_ENV": "test",
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
        },
    )
    for worker in ("agent-worker", "tool-worker"):
        assert defaulted["services"][worker]["environment"]["APP_ENV"] == "dev"
        assert explicit["services"][worker]["environment"]["APP_ENV"] == "test"


def test_shared_contract_rejects_adapter_supplemental_group_authority() -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    rendered["services"]["rootless-docker-transport"]["group_add"] = ["20002"]

    with pytest.raises(ValueError, match=r"adapter.*group"):
        contract.assert_rendered_contract(rendered, mode="rootless", dev=False)


def test_renderer_disables_dotenv_and_scrubs_inherited_mode_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = _load_contract()
    for filename in ("compose.yaml", "compose.dev.yaml", "compose.rootless.yaml"):
        shutil.copy2(ROOT / filename, tmp_path / filename)
    (tmp_path / ".env").write_text(
        "APP_ENV=poisoned-from-dotenv\nSANDBOX_DOCKER_GID=poisoned-gid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    monkeypatch.setenv("APP_ENV", "poisoned-from-process")
    monkeypatch.setenv("COMPOSE_FILE", "attacker.yaml")
    monkeypatch.setenv("COMPOSE_PROFILES", "attacker")
    rendered = contract.render_compose(
        "rootless",
        dev=True,
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    serialized = json.dumps(rendered, sort_keys=True)
    assert "poisoned" not in serialized
    assert rendered["services"]["agent-worker"]["environment"]["APP_ENV"] == "dev"
    assert rendered["services"]["tool-worker"]["environment"]["APP_ENV"] == "dev"


@pytest.mark.parametrize(
    ("files", "mode", "env"),
    [
        (("compose.yaml",), "rootless", {}),
        (
            ("compose.yaml", "compose.rootful.yaml", "compose.rootless.yaml"),
            "rootless",
            {
                "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
                "SANDBOX_DOCKER_GID": "10001",
                "SANDBOX_DOCKER_SOCKET_HOST": "/var/run/docker.sock",
            },
        ),
        (
            ("compose.yaml", "compose.rootless.yaml", "compose.rootful.yaml"),
            "rootful",
            {
                "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
                "SANDBOX_DOCKER_GID": "10001",
                "SANDBOX_DOCKER_SOCKET_HOST": "/var/run/docker.sock",
            },
        ),
    ],
)
def test_shared_contract_rejects_missing_or_merged_authority_vectors(
    files: tuple[str, ...],
    mode: str,
    env: dict[str, str],
) -> None:
    contract = _load_contract()
    rendered = _raw_render(*files, env=env)
    with pytest.raises(ValueError, match=r"authority|mode|overlay"):
        contract.assert_rendered_contract(
            rendered,
            mode=mode,
            dev=False,
            expected_rootful_gid=10001 if mode == "rootful" else None,
            expected_socket_source="/var/run/docker.sock" if mode == "rootful" else None,
        )


def _root_owned_socket_lstat(path: Path) -> SimpleNamespace:
    observed = path.lstat()
    return SimpleNamespace(st_mode=observed.st_mode, st_uid=0, st_gid=ROOTFUL_TEST_GID)


def _non_root_socket_lstat(path: Path) -> SimpleNamespace:
    observed = path.lstat()
    return SimpleNamespace(
        st_mode=observed.st_mode,
        st_uid=10001,
        st_gid=ROOTFUL_TEST_GID,
    )


def test_rootful_preflight_accepts_a_real_unix_socket_without_mutating_it(tmp_path: Path) -> None:
    contract = _load_contract()
    with tempfile.TemporaryDirectory(prefix="p10-", dir="/tmp") as short_directory:
        socket_path = Path(short_directory) / "docker.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            before = socket_path.lstat()
            resolved, gid = contract.validate_rootful_socket(
                str(socket_path),
                str(ROOTFUL_TEST_GID),
                _lstat=_root_owned_socket_lstat,
            )
            after = socket_path.lstat()
    assert resolved == str(socket_path)
    assert gid == ROOTFUL_TEST_GID
    assert (after.st_mode, after.st_uid, after.st_gid) == (
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("relative", "absolute"),
        ("missing", "inspect"),
        ("regular", "Unix socket"),
        ("symlink", "symlink"),
        ("uid", "UID 0"),
        ("gid-zero", "positive"),
        ("gid-mismatch", "does not match"),
        ("gid-invalid", "positive"),
    ],
)
def test_rootful_preflight_rejects_every_invalid_socket_shape(
    case: str,
    message: str,
    tmp_path: Path,
) -> None:
    contract = _load_contract()
    with tempfile.TemporaryDirectory(prefix="p10-", dir="/tmp") as short_directory:
        short_root = Path(short_directory)
        socket_path = short_root / "docker.sock"
        regular_path = short_root / "regular"
        regular_path.write_text("not a socket", encoding="utf-8")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            gid = ROOTFUL_TEST_GID
            target = str(socket_path)
            configured_gid = str(gid)
            lstat: Any = _root_owned_socket_lstat
            if case == "relative":
                target = "docker.sock"
            elif case == "missing":
                target = str(short_root / "missing.sock")
            elif case == "regular":
                target = str(regular_path)
            elif case == "symlink":
                link = short_root / "link.sock"
                link.symlink_to(socket_path)
                target = str(link)
                lstat = Path.lstat
            elif case == "uid":
                lstat = _non_root_socket_lstat
            elif case == "gid-zero":
                configured_gid = "0"
            elif case == "gid-mismatch":
                configured_gid = str(gid + 1)
            elif case == "gid-invalid":
                configured_gid = "not-an-integer"
            with pytest.raises(ValueError, match=message):
                contract.validate_rootful_socket(target, configured_gid, _lstat=lstat)


def test_cli_owns_the_exact_mode_file_vector_and_no_file_injection() -> None:
    contract = _load_contract()
    assert contract.compose_files("rootless") == ("compose.yaml", "compose.rootless.yaml")
    assert contract.compose_files("rootful") == ("compose.yaml", "compose.rootful.yaml")
    assert contract.compose_files("rootless", dev=True) == (
        "compose.yaml",
        "compose.dev.yaml",
        "compose.rootless.yaml",
    )
    assert contract.compose_files("rootful", dev=True) == (
        "compose.yaml",
        "compose.dev.yaml",
        "compose.rootful.yaml",
    )
    with pytest.raises(SystemExit):
        contract.main(["--mode", "rootless", "-f", "compose.rootful.yaml"])


def test_environment_example_has_no_active_app_env_or_mode_authority() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active_keys = {
        line.split("=", 1)[0] for line in lines if line and not line.startswith("#") and "=" in line
    }
    assert "APP_ENV" not in active_keys
    assert "SANDBOX_DOCKER_GID" not in active_keys
    assert "SANDBOX_DOCKER_SOCKET_HOST" not in active_keys
    assert "PHASE10_ROOTLESS_DOCKER_SOCKET" not in active_keys


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (
            "rootful",
            {
                "api",
                "web",
                "workflow-worker",
                "agent-worker",
                "tool-worker",
                "sandbox-runner",
                "event-worker",
                "postgres",
                "nats",
                "temporal",
            },
        ),
        (
            "rootless",
            {
                "api",
                "web",
                "workflow-worker",
                "agent-worker",
                "tool-worker",
                "sandbox-runner",
                "rootless-docker-transport",
                "event-worker",
                "postgres",
                "nats",
                "temporal",
            },
        ),
    ],
)
def test_integration_required_services_follow_selected_mode(
    mode: str,
    expected: set[str],
) -> None:
    from tests.integration.conftest import required_services_for_mode

    assert required_services_for_mode(mode) == expected


def test_ps_all_parser_rejects_missing_blank_exited_and_unhealthy_rows() -> None:
    from tests.integration.test_stack_health import unhealthy_expected_services

    expected = {"agent-worker", "tool-worker", "sandbox-runner"}
    healthy = json.dumps(
        [
            {"Service": service, "State": "running", "Health": "healthy"}
            for service in sorted(expected)
        ]
    )
    assert unhealthy_expected_services(healthy, expected) == {}

    cases = {
        "missing": json.dumps(
            [{"Service": "agent-worker", "State": "running", "Health": "healthy"}]
        ),
        "blank": json.dumps(
            [
                {"Service": "agent-worker", "State": "", "Health": ""},
                {"Service": "tool-worker", "State": "running", "Health": "healthy"},
                {"Service": "sandbox-runner", "State": "running", "Health": "healthy"},
            ]
        ),
        "exited": json.dumps(
            [
                {"Service": "agent-worker", "State": "exited", "Health": "healthy"},
                {"Service": "tool-worker", "State": "running", "Health": "healthy"},
                {"Service": "sandbox-runner", "State": "running", "Health": "healthy"},
            ]
        ),
        "unhealthy": json.dumps(
            [
                {"Service": "agent-worker", "State": "running", "Health": "unhealthy"},
                {"Service": "tool-worker", "State": "running", "Health": "healthy"},
                {"Service": "sandbox-runner", "State": "running", "Health": "healthy"},
            ]
        ),
    }
    for label, output in cases.items():
        assert unhealthy_expected_services(output, expected), label


def test_dynamic_job_boundary_parser_requires_every_denial() -> None:
    from tests.integration.test_phase6_security import (
        FORBIDDEN_JOB_DNS_NAMES,
        FORBIDDEN_JOB_NETWORK_NAMES,
        FORBIDDEN_JOB_SOCKET_PATHS,
        assert_job_boundary_denials,
    )

    lines = [f"socket-denied:{path}" for path in FORBIDDEN_JOB_SOCKET_PATHS]
    lines.extend(f"dns-denied:{name}" for name in FORBIDDEN_JOB_DNS_NAMES)
    lines.extend(f"network-denied:{name}" for name in FORBIDDEN_JOB_NETWORK_NAMES)
    lines.extend(
        [
            "adapter-tcp-denied",
            "sandbox-docker-env-denied",
            "group-add-denied",
        ]
    )
    output = "\n".join(lines)
    assert_job_boundary_denials(output)
    for line in lines:
        with pytest.raises(AssertionError):
            assert_job_boundary_denials(output.replace(line, "", 1))
