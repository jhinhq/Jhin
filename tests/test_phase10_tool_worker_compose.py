"""Executable contract for the Phase 10 tool-worker Compose authority boundary."""

from __future__ import annotations

import copy
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
        expected_app_env: str,
        expected_rootful_gid: int | None = None,
        expected_socket_source: str | None = None,
    ) -> None: ...

    @staticmethod
    def assert_source_contract() -> None: ...

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
@pytest.mark.parametrize(
    ("dev", "app_env_override", "expected_app_env"),
    [
        (False, None, "production"),
        (True, None, "dev"),
        (True, "test", "test"),
    ],
)
def test_shared_contract_accepts_every_supported_render(
    mode: str,
    dev: bool,
    app_env_override: str | None,
    expected_app_env: str,
    tmp_path: Path,
) -> None:
    contract = _load_contract()
    if mode == "rootless":
        env = {
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_DOCKER_GID": ROOTLESS_GID_CANARY,
        }
        if app_env_override is not None:
            env["APP_ENV"] = app_env_override
        rendered = contract.render_compose(mode, dev=dev, env=env)
        contract.assert_rendered_contract(
            rendered,
            mode=mode,
            dev=dev,
            expected_app_env=expected_app_env,
            expected_socket_source=ROOTLESS_SOCKET,
        )
        assert ROOTLESS_GID_CANARY not in json.dumps(rendered, sort_keys=True)
        return

    with tempfile.TemporaryDirectory(prefix="p10-", dir="/tmp") as short_directory:
        socket_path = Path(short_directory) / "docker.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(socket_path))
            gid = ROOTFUL_TEST_GID
            env = {
                "SANDBOX_DOCKER_GID": str(gid),
                "SANDBOX_DOCKER_SOCKET_HOST": str(socket_path),
            }
            if app_env_override is not None:
                env["APP_ENV"] = app_env_override
            rendered = contract.render_compose(mode, dev=dev, env=env)
    contract.assert_rendered_contract(
        rendered,
        mode=mode,
        dev=dev,
        expected_app_env=expected_app_env,
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
        contract.assert_rendered_contract(
            rendered,
            mode="rootless",
            dev=False,
            expected_app_env="production",
            expected_socket_source=ROOTLESS_SOCKET,
        )


def _set_nested(config: dict[str, Any], *path_and_value: Any) -> None:
    *path, value = path_and_value
    target: Any = config
    for key in path[:-1]:
        if isinstance(target, dict) and key not in target:
            target[key] = {}
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        (
            "runner CMD true",
            ("services", "sandbox-runner", "healthcheck", "test"),
            ["CMD", "true"],
        ),
        (
            "runner 24h health interval",
            ("services", "sandbox-runner", "healthcheck", "interval"),
            "24h",
        ),
        (
            "adapter CMD true",
            ("services", "rootless-docker-transport", "healthcheck", "test"),
            ["CMD", "true"],
        ),
        (
            "adapter 24h health timeout",
            ("services", "rootless-docker-transport", "healthcheck", "timeout"),
            "24h",
        ),
        (
            "api runner network",
            ("services", "api", "networks", "runner"),
            None,
        ),
        (
            "api implicit default network",
            ("services", "api", "networks", "default"),
            None,
        ),
        (
            "runner database authority",
            ("services", "sandbox-runner", "environment", "DATABASE_URL"),
            "postgresql://postgres/jhin",
        ),
        (
            "runner NATS authority",
            ("services", "sandbox-runner", "environment", "NATS_URL"),
            "nats://nats:4222",
        ),
        (
            "adapter master key authority",
            ("services", "rootless-docker-transport", "environment", "MASTER_KEY_FILE"),
            "/run/secrets/jhin_master_key",
        ),
        (
            "rootless host socket source",
            ("services", "rootless-docker-transport", "volumes", 0, "source"),
            "/var/run/docker.sock",
        ),
        (
            "global runner network user",
            ("services", "web", "networks", "runner"),
            None,
        ),
        (
            "global engine network user",
            ("services", "api", "networks", "engine"),
            None,
        ),
        ("runner user", ("services", "sandbox-runner", "user"), "0:0"),
        ("adapter user", ("services", "rootless-docker-transport", "user"), "10001:10001"),
    ],
)
def test_shared_contract_rejects_every_reviewed_security_mutation(
    label: str,
    path: tuple[Any, ...],
    value: Any,
) -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    mutated = copy.deepcopy(rendered)
    _set_nested(mutated, *path, value)

    with pytest.raises(ValueError, match=r"health|network|environment|socket|user"):
        contract.assert_rendered_contract(
            mutated,
            mode="rootless",
            dev=False,
            expected_app_env="production",
            expected_socket_source=ROOTLESS_SOCKET,
        )


@pytest.mark.parametrize("filename", ["compose.rootful.yaml", "compose.rootless.yaml"])
def test_source_contract_rejects_bind_that_can_create_a_host_path(
    filename: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    for source_name in ("compose.rootful.yaml", "compose.rootless.yaml"):
        shutil.copy2(ROOT / source_name, tmp_path / source_name)
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    contract.assert_source_contract()

    source_path = tmp_path / filename
    source = source_path.read_text(encoding="utf-8")
    unsafe = source.replace("bind:\n          create_host_path: false", "bind: {}", 1)
    assert unsafe != source
    source_path.write_text(unsafe, encoding="utf-8")

    with pytest.raises(ValueError, match=r"create_host_path"):
        contract.assert_source_contract()


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
            expected_app_env="production",
            expected_rootful_gid=10001 if mode == "rootful" else None,
            expected_socket_source=(
                "/var/run/docker.sock" if mode == "rootful" else ROOTLESS_SOCKET
            ),
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


@pytest.mark.parametrize("mode", ["rootful", "rootless"])
@pytest.mark.parametrize(
    ("dev", "inherited_app_env", "expected_app_env"),
    [
        (False, "test", "production"),
        (True, None, "dev"),
        (True, "test", "test"),
    ],
)
def test_cli_passes_the_explicit_expected_app_env_for_every_vector(
    mode: str,
    dev: bool,
    inherited_app_env: str | None,
    expected_app_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    observed: dict[str, Any] = {}
    rendered: dict[str, Any] = {"services": {}}

    if inherited_app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", inherited_app_env)
    monkeypatch.setenv("PHASE10_ROOTLESS_DOCKER_SOCKET", ROOTLESS_SOCKET)
    monkeypatch.setenv("SANDBOX_DOCKER_SOCKET_HOST", "/host/docker.sock")
    monkeypatch.setenv("SANDBOX_DOCKER_GID", str(ROOTFUL_TEST_GID))
    monkeypatch.setattr(
        contract,
        "validate_rootful_socket",
        lambda _path, _gid: ("/verified/docker.sock", ROOTFUL_TEST_GID),
    )

    def fake_render(
        selected_mode: str,
        *,
        dev: bool,
        env: dict[str, str] | None,
    ) -> dict[str, Any]:
        observed["render"] = (selected_mode, dev, env)
        return rendered

    def fake_assert(config: dict[str, Any], **kwargs: Any) -> None:
        observed["assert"] = (config, kwargs)

    monkeypatch.setattr(contract, "render_compose", fake_render)
    monkeypatch.setattr(contract, "assert_rendered_contract", fake_assert)

    argv = ["--mode", mode]
    if dev:
        argv.append("--dev")
    assert contract.main(argv) == 0

    expected_env = (
        {
            "SANDBOX_DOCKER_SOCKET_HOST": "/verified/docker.sock",
            "SANDBOX_DOCKER_GID": str(ROOTFUL_TEST_GID),
        }
        if mode == "rootful"
        else {"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET}
    )
    if dev and inherited_app_env is not None:
        expected_env["APP_ENV"] = inherited_app_env
    assert observed["render"] == (mode, dev, expected_env)
    assert observed["assert"] == (
        rendered,
        {
            "mode": mode,
            "dev": dev,
            "expected_app_env": expected_app_env,
            "expected_rootful_gid": ROOTFUL_TEST_GID if mode == "rootful" else None,
            "expected_socket_source": (
                "/verified/docker.sock" if mode == "rootful" else ROOTLESS_SOCKET
            ),
        },
    )


def test_environment_example_has_no_active_app_env_or_mode_authority() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active_keys = {
        line.split("=", 1)[0] for line in lines if line and not line.startswith("#") and "=" in line
    }
    assert "APP_ENV" not in active_keys
    assert "SANDBOX_DOCKER_GID" not in active_keys
    assert "SANDBOX_DOCKER_SOCKET_HOST" not in active_keys
    assert "PHASE10_ROOTLESS_DOCKER_SOCKET" not in active_keys
    assert "api/agent-worker/tool-worker" in "\n".join(lines)


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


@pytest.mark.parametrize("mode", ["rootful", "rootless"])
def test_integration_compose_command_uses_base_dev_and_one_mode_overlay(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import conftest as harness

    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="api\n", stderr="")

    monkeypatch.setenv("PHASE10_SOCKET_MODE", mode)
    monkeypatch.setenv("JHIN_TEST_COMPOSE_PROJECT", "jhin-phase10-contract")
    monkeypatch.setattr(subprocess, "run", fake_run)

    harness.compose("ps", "--all", timeout=7.0)

    assert observed == {
        "command": [
            "docker",
            "compose",
            "-p",
            "jhin-phase10-contract",
            "-f",
            "compose.yaml",
            "-f",
            "compose.dev.yaml",
            "-f",
            f"compose.{mode}.yaml",
            "ps",
            "--all",
        ],
        "kwargs": {
            "cwd": harness.REPO_ROOT,
            "capture_output": True,
            "text": True,
            "timeout": 7.0,
            "check": True,
        },
    }


def test_integration_compose_requires_an_explicit_socket_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import conftest as harness

    monkeypatch.delenv("PHASE10_SOCKET_MODE", raising=False)
    with pytest.raises(ValueError, match="PHASE10_SOCKET_MODE"):
        harness.compose("ps")
    with pytest.raises(ValueError, match="PHASE10_SOCKET_MODE"):
        harness.selected_compose_mode()


def test_ps_all_parser_accepts_compose_array_and_thirteen_line_ndjson() -> None:
    from tests.integration.test_stack_health import unhealthy_expected_services

    services = {
        "agent-worker",
        "api",
        "event-worker",
        "fake-github",
        "fake-provider",
        "nats",
        "postgres",
        "sandbox-runner",
        "temporal",
        "temporal-ui",
        "tool-worker",
        "web",
        "workflow-worker",
    }
    rows = [
        {"Service": service, "State": "running", "Health": "healthy"}
        for service in sorted(services)
    ]
    assert len(rows) == 13
    assert unhealthy_expected_services(json.dumps(rows), services) == {}
    assert unhealthy_expected_services("\n".join(map(json.dumps, rows)), services) == {}


def test_ps_all_parser_rejects_invalid_identity_rows_closed() -> None:
    from tests.integration.test_stack_health import unhealthy_expected_services

    valid = {"Service": "api", "State": "running", "Health": "healthy"}
    invalid_outputs = {
        "malformed": json.dumps(valid) + "\n{not-json",
        "nonobject": json.dumps([valid, "not-an-object"]),
        "duplicate": json.dumps([valid, valid]),
        "missing identity": json.dumps([{"State": "running", "Health": "healthy"}]),
        "blank identity": json.dumps([{"Service": " ", "State": "running", "Health": "healthy"}]),
    }
    for _label, output in invalid_outputs.items():
        with pytest.raises(ValueError, match=r"Compose ps|identity|duplicate"):
            unhealthy_expected_services(output, {"api"})


def test_ps_all_parser_rejects_missing_blank_exited_and_unhealthy_services() -> None:
    from tests.integration.test_stack_health import unhealthy_expected_services

    expected = {"agent-worker", "tool-worker", "sandbox-runner"}

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
        FORBIDDEN_JOB_SOCKET_PATHS,
        assert_job_boundary_denials,
    )

    lines = [f"socket-denied:{path}" for path in FORBIDDEN_JOB_SOCKET_PATHS]
    lines.extend(f"dns-denied:{name}" for name in FORBIDDEN_JOB_DNS_NAMES)
    lines.extend(["adapter-tcp-denied", "sandbox-docker-env-denied", "boundary-probe-complete"])
    output = "\n".join(lines)
    assert_job_boundary_denials(output)
    for line in lines:
        with pytest.raises(AssertionError):
            assert_job_boundary_denials(output.replace(line, "", 1))


@pytest.mark.parametrize(
    ("network_policy", "network_mode"),
    [("none", "none"), ("internet", "jhin_sandbox")],
)
def test_host_inspection_accepts_exact_job_boundary(
    network_policy: str,
    network_mode: str,
) -> None:
    from tests.integration.test_phase6_security import assert_job_container_boundary

    job_id = "phase10-live-boundary"
    inspected = {
        "Config": {
            "User": "1000:1000",
            "Env": ["HOME=/workspace", "SAFE=value"],
            "Labels": {"jhin.sandbox.job": job_id},
        },
        "HostConfig": {
            "NetworkMode": network_mode,
            "GroupAdd": None,
            "Binds": None,
        },
        "Mounts": [],
    }

    assert_job_container_boundary(
        inspected,
        job_id=job_id,
        network_policy=network_policy,
        sandbox_network="jhin_sandbox",
    )


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        ("control network", ("HostConfig", "NetworkMode"), "runner"),
        ("supplemental group", ("HostConfig", "GroupAdd"), ["10001"]),
        ("job root user", ("Config", "User"), "0:0"),
        ("Docker environment", ("Config", "Env"), ["DOCKER_HOST=tcp://docker:2375"]),
        (
            "adapter environment authority",
            ("Config", "Env"),
            ["AUTH=http://rootless-docker-transport:2375"],
        ),
        ("host bind", ("HostConfig", "Binds"), ["/var/run/docker.sock:/authority"]),
        (
            "socket mount",
            ("Mounts",),
            [{"Source": "/var/run/docker.sock", "Destination": "/authority"}],
        ),
    ],
)
def test_host_inspection_rejects_every_job_authority_mutation(
    label: str,
    path: tuple[Any, ...],
    value: Any,
) -> None:
    from tests.integration.test_phase6_security import assert_job_container_boundary

    job_id = "phase10-live-boundary"
    inspected = {
        "Config": {
            "User": "1000:1000",
            "Env": ["HOME=/workspace"],
            "Labels": {"jhin.sandbox.job": job_id},
        },
        "HostConfig": {"NetworkMode": "none", "GroupAdd": None, "Binds": None},
        "Mounts": [],
    }
    mutated = copy.deepcopy(inspected)
    _set_nested(mutated, *path, value)

    with pytest.raises(AssertionError, match=r"network|group|user|environment|authority|mount"):
        assert_job_container_boundary(
            mutated,
            job_id=job_id,
            network_policy="none",
            sandbox_network="jhin_sandbox",
        )
