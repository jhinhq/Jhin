"""Executable contract for the Phase 10 tool-worker Compose authority boundary."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
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
DEFAULT_SANDBOX_NETWORK = "jhin_sandbox"
UNIQUE_SANDBOX_NETWORK = "jhin-phase10-contract-sandbox"
MAX_SANDBOX_NETWORK_LENGTH = 63


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
        expected_sandbox_network: str,
        expected_rootful_gid: int | None = None,
        expected_socket_source: str | None = None,
    ) -> None: ...

    @staticmethod
    def assert_source_contract() -> None: ...

    @staticmethod
    def validate_sandbox_network(value: str) -> str: ...

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
            "SANDBOX_NETWORK": UNIQUE_SANDBOX_NETWORK,
        }
        if app_env_override is not None:
            env["APP_ENV"] = app_env_override
        rendered = contract.render_compose(mode, dev=dev, env=env)
        contract.assert_rendered_contract(
            rendered,
            mode=mode,
            dev=dev,
            expected_app_env=expected_app_env,
            expected_sandbox_network=UNIQUE_SANDBOX_NETWORK,
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
                "SANDBOX_NETWORK": UNIQUE_SANDBOX_NETWORK,
            }
            if app_env_override is not None:
                env["APP_ENV"] = app_env_override
            rendered = contract.render_compose(mode, dev=dev, env=env)
    contract.assert_rendered_contract(
        rendered,
        mode=mode,
        dev=dev,
        expected_app_env=expected_app_env,
        expected_sandbox_network=UNIQUE_SANDBOX_NETWORK,
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


def test_default_sandbox_network_is_explicitly_asserted_in_production_and_dev() -> None:
    contract = _load_contract()
    for dev in (False, True):
        rendered = contract.render_compose(
            "rootless",
            dev=dev,
            env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
        )
        contract.assert_rendered_contract(
            rendered,
            mode="rootless",
            dev=dev,
            expected_app_env="dev" if dev else "production",
            expected_sandbox_network=DEFAULT_SANDBOX_NETWORK,
            expected_socket_source=ROOTLESS_SOCKET,
        )
        assert (
            rendered["services"]["sandbox-runner"]["environment"]["SANDBOX_NETWORK"]
            == DEFAULT_SANDBOX_NETWORK
        )
        if dev:
            assert rendered["networks"]["sandbox"]["name"] == DEFAULT_SANDBOX_NETWORK
        else:
            assert "sandbox" not in rendered["networks"]


@pytest.mark.parametrize("dev", [False, True])
def test_every_rendered_boundary_network_is_an_owned_bridge(dev: bool) -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        dev=dev,
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    expected = {"edge", "control", "data", "runner", "engine"}
    if dev:
        expected.add("sandbox")
    assert set(rendered["networks"]) == expected
    for name in expected:
        network = rendered["networks"][name]
        assert network["driver"] == "bridge", name
        assert network.get("external", False) is False, name


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "host",
        " HOST ",
        "bridge",
        "Bridge",
        "none",
        "\tnone\n",
        "default",
        "runner",
        "RUNNER",
        "engine",
        " Engine ",
        "container:abc123",
        " Container:abc123 ",
        "container : abc123",
        "container:\nabc123",
        "container \n : abc123",
        "con\ntainer:abc123",
        " jhin_unique_sandbox ",
        "Jhin_unique_sandbox",
        "_jhin_sandbox",
        "-jhin_sandbox",
        ".jhin_sandbox",
        "jhin/sandbox",
        "jhin:sandbox",
        "jhin sandbox",
        "jhin\nsandbox",
        "jhin\x00sandbox",
        "a" * (MAX_SANDBOX_NETWORK_LENGTH + 1),
    ],
)
def test_sandbox_network_validation_rejects_reserved_or_ambiguous_values(value: str) -> None:
    contract = _load_contract()

    with pytest.raises(ValueError, match=r"SANDBOX_NETWORK|sandbox network"):
        contract.validate_sandbox_network(value)


def test_sandbox_network_validation_preserves_a_unique_explicit_name() -> None:
    contract = _load_contract()

    assert contract.validate_sandbox_network(UNIQUE_SANDBOX_NETWORK) == UNIQUE_SANDBOX_NETWORK
    maximum = "a" * MAX_SANDBOX_NETWORK_LENGTH
    assert contract.validate_sandbox_network(maximum) == maximum


@pytest.mark.parametrize(
    ("label", "changes", "expected_network"),
    [
        (
            "runner expected mismatch",
            (("services", "sandbox-runner", "environment", "SANDBOX_NETWORK", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "rendered sandbox mismatch",
            (("networks", "sandbox", "name", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "rendered sandbox declaration missing",
            (("networks", "sandbox", None),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "runner physical name collision",
            (
                ("services", "sandbox-runner", "environment", "SANDBOX_NETWORK", "jhin_runner"),
                ("networks", "sandbox", "name", "jhin_runner"),
            ),
            "jhin_runner",
        ),
        (
            "engine physical name collision",
            (
                ("services", "sandbox-runner", "environment", "SANDBOX_NETWORK", "jhin_engine"),
                ("networks", "sandbox", "name", "jhin_engine"),
            ),
            "jhin_engine",
        ),
        (
            "api sandbox membership",
            (("services", "api", "networks", "sandbox", None),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "event worker sandbox membership",
            (("services", "event-worker", "networks", "sandbox", None),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "fake GitHub sandbox membership missing",
            (("services", "fake-github", "networks", "sandbox", None),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "physical name collision",
            (("networks", "control", "name", "jhin_data"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "empty runner token",
            (("services", "sandbox-runner", "environment", "SANDBOX_RUNNER_TOKEN", ""),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "wrong runner token",
            (("services", "sandbox-runner", "environment", "SANDBOX_RUNNER_TOKEN", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "wrong tool token",
            (("services", "tool-worker", "environment", "SANDBOX_RUNNER_TOKEN", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "wrong runner image",
            (("services", "sandbox-runner", "environment", "SANDBOX_DEFAULT_IMAGE", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
        (
            "wrong tool image",
            (("services", "tool-worker", "environment", "SANDBOX_DEFAULT_IMAGE", "wrong"),),
            UNIQUE_SANDBOX_NETWORK,
        ),
    ],
)
def test_shared_contract_rejects_network_token_and_image_mutations(
    label: str,
    changes: tuple[tuple[Any, ...], ...],
    expected_network: str,
) -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        dev=True,
        env={
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_NETWORK": UNIQUE_SANDBOX_NETWORK,
        },
    )
    mutated = copy.deepcopy(rendered)
    for change in changes:
        if label == "fake GitHub sandbox membership missing":
            del mutated["services"]["fake-github"]["networks"]["sandbox"]
        else:
            _set_nested(mutated, *change)

    with pytest.raises(ValueError, match=r"network|token|image|environment"):
        contract.assert_rendered_contract(
            mutated,
            mode="rootless",
            dev=True,
            expected_app_env="dev",
            expected_sandbox_network=expected_network,
            expected_socket_source=ROOTLESS_SOCKET,
        )


def test_shared_contract_rejects_expected_sandbox_network_mismatch() -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        dev=True,
        env={
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_NETWORK": UNIQUE_SANDBOX_NETWORK,
        },
    )

    with pytest.raises(ValueError, match=r"network"):
        contract.assert_rendered_contract(
            rendered,
            mode="rootless",
            dev=True,
            expected_app_env="dev",
            expected_sandbox_network="other-unique-sandbox",
            expected_socket_source=ROOTLESS_SOCKET,
        )


@pytest.mark.parametrize(
    ("network_name", "field", "value"),
    [
        (network_name, field, value)
        for network_name in ("edge", "control", "data", "runner", "engine", "sandbox")
        for field, value in (("driver", "host"), ("external", True))
    ],
)
def test_shared_contract_rejects_nonbridge_or_external_boundary_networks(
    network_name: str,
    field: str,
    value: Any,
) -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        dev=True,
        env={
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_NETWORK": UNIQUE_SANDBOX_NETWORK,
        },
    )
    rendered["networks"][network_name][field] = value

    with pytest.raises(ValueError, match=r"bridge|external|network"):
        contract.assert_rendered_contract(
            rendered,
            mode="rootless",
            dev=True,
            expected_app_env="dev",
            expected_sandbox_network=UNIQUE_SANDBOX_NETWORK,
            expected_socket_source=ROOTLESS_SOCKET,
        )


def test_production_contract_rejects_any_logical_sandbox_network_user() -> None:
    contract = _load_contract()
    rendered = contract.render_compose(
        "rootless",
        env={"PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET},
    )
    rendered["services"]["api"]["networks"]["sandbox"] = None

    with pytest.raises(ValueError, match=r"sandbox.*network|network.*sandbox"):
        contract.assert_rendered_contract(
            rendered,
            mode="rootless",
            dev=False,
            expected_app_env="production",
            expected_sandbox_network=DEFAULT_SANDBOX_NETWORK,
            expected_socket_source=ROOTLESS_SOCKET,
        )


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
            expected_sandbox_network=DEFAULT_SANDBOX_NETWORK,
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
            expected_sandbox_network=DEFAULT_SANDBOX_NETWORK,
            expected_socket_source=ROOTLESS_SOCKET,
        )


def _copy_source_contract(tmp_path: Path) -> None:
    for source_name in ("compose.yaml", "compose.rootful.yaml", "compose.rootless.yaml"):
        shutil.copy2(ROOT / source_name, tmp_path / source_name)


def _safe_bind_fragment(filename: str) -> str:
    if filename == "compose.rootful.yaml":
        source = "${SANDBOX_DOCKER_SOCKET_HOST:?set verified absolute Docker socket}"
        target = "/run/jhin/docker.sock"
    else:
        source = "${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}"
        target = "/run/host/docker.sock"
    return (
        "      - type: bind\n"
        f"        source: {source}\n"
        f"        target: {target}\n"
        "        bind:\n"
        "          create_host_path: false"
    )


@pytest.mark.parametrize("filename", ["compose.rootful.yaml", "compose.rootless.yaml"])
def test_semantic_source_contract_ignores_safe_text_decoys(
    filename: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    contract.assert_source_contract()

    source_path = tmp_path / filename
    source = source_path.read_text(encoding="utf-8")
    unsafe = source.replace("bind:\n          create_host_path: false", "bind: {}", 1)
    assert unsafe != source
    unsafe += f"\nx-phase10-safe-text-decoy: |\n{_safe_bind_fragment(filename)}\n"
    unsafe += "# create_host_path: false cannot repair an unsafe node\n"
    source_path.write_text(unsafe, encoding="utf-8")

    with pytest.raises(ValueError, match=r"create_host_path"):
        contract.assert_source_contract()


@pytest.mark.parametrize("filename", ["compose.rootful.yaml", "compose.rootless.yaml"])
@pytest.mark.parametrize("duplicate", ["top-level services", "bind key"])
def test_semantic_source_contract_rejects_duplicate_mapping_keys(
    filename: str,
    duplicate: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    source_path = tmp_path / filename
    source = source_path.read_text(encoding="utf-8")
    if duplicate == "top-level services":
        source += "\nservices:\n  phase10-duplicate-service: {}\n"
    else:
        source = source.replace(
            "bind:\n          create_host_path: false",
            "bind:\n          create_host_path: false\n        bind: {}",
            1,
        )
    source_path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match=r"duplicate"):
        contract.assert_source_contract()


@pytest.mark.parametrize("filename", ["compose.rootful.yaml", "compose.rootless.yaml"])
def test_semantic_source_contract_rejects_duplicate_socket_volume_nodes(
    filename: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    source_path = tmp_path / filename
    source = source_path.read_text(encoding="utf-8")
    source = source.replace(
        "      - type: bind\n",
        "      - &phase10-socket-bind\n        type: bind\n",
        1,
    )
    source = source.replace(
        "          create_host_path: false",
        "          create_host_path: false\n      - *phase10-socket-bind",
        1,
    )
    source_path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match=r"socket bind|volume|exactly one"):
        contract.assert_source_contract()


@pytest.mark.parametrize("style", ["alias", "merge"])
def test_semantic_source_contract_accepts_unambiguous_safe_anchors(
    style: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    shutil.copy2(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    volume_reference = "*phase10-safe-bind" if style == "alias" else "<<: *phase10-safe-bind"
    overlays = {
        "compose.rootful.yaml": (
            "x-phase10-safe-bind: &phase10-safe-bind\n"
            "  type: bind\n"
            "  source: ${SANDBOX_DOCKER_SOCKET_HOST:?set verified absolute Docker socket}\n"
            "  target: /run/jhin/docker.sock\n"
            "  bind:\n"
            "    create_host_path: false\n"
            "services:\n"
            "  sandbox-runner:\n"
            "    volumes:\n"
            f"      - {volume_reference}\n"
        ),
        "compose.rootless.yaml": (
            "x-phase10-safe-bind: &phase10-safe-bind\n"
            "  type: bind\n"
            "  source: ${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}\n"
            "  target: /run/host/docker.sock\n"
            "  bind:\n"
            "    create_host_path: false\n"
            "services:\n"
            "  rootless-docker-transport:\n"
            "    volumes:\n"
            f"      - {volume_reference}\n"
            "networks:\n"
            "  engine:\n"
            "    driver: bridge\n"
            "    external: false\n"
            "    internal: true\n"
        ),
    }
    for filename, source in overlays.items():
        (tmp_path / filename).write_text(source, encoding="utf-8")
    monkeypatch.setattr(contract, "ROOT", tmp_path)

    contract.assert_source_contract()


def test_semantic_source_contract_rejects_unsafe_anchor_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    source = (
        "x-phase10-safe-bind: &phase10-safe-bind\n"
        "  type: bind\n"
        "  source: ${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}\n"
        "  target: /run/host/docker.sock\n"
        "  bind:\n"
        "    create_host_path: false\n"
        "services:\n"
        "  rootless-docker-transport:\n"
        "    volumes:\n"
        "      - <<: *phase10-safe-bind\n"
        "        bind: {}\n"
        "networks:\n"
        "  engine:\n"
        "    driver: bridge\n"
        "    external: false\n"
        "    internal: true\n"
    )
    (tmp_path / "compose.rootless.yaml").write_text(source, encoding="utf-8")
    monkeypatch.setattr(contract, "ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"create_host_path"):
        contract.assert_source_contract()


def test_semantic_source_contract_rejects_network_interpolation_decoys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    compose_path = tmp_path / "compose.yaml"
    source = compose_path.read_text(encoding="utf-8")
    source = source.replace(
        "SANDBOX_NETWORK: ${SANDBOX_NETWORK:-jhin_sandbox}",
        "SANDBOX_NETWORK: unsafe-network",
        1,
    )
    source = source.replace(
        "name: ${SANDBOX_NETWORK:-jhin_sandbox}",
        "name: unsafe-network",
        1,
    )
    source += (
        "\nx-phase10-network-decoy: |\n"
        "  SANDBOX_NETWORK: ${SANDBOX_NETWORK:-jhin_sandbox}\n"
        "  name: ${SANDBOX_NETWORK:-jhin_sandbox}\n"
    )
    compose_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(contract, "ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"SANDBOX_NETWORK|sandbox network"):
        contract.assert_source_contract()


@pytest.mark.parametrize(
    ("filename", "network_name", "replacement"),
    [
        (
            "compose.yaml",
            "sandbox",
            "  sandbox:\n    name: ${SANDBOX_NETWORK:-jhin_sandbox}\n    driver: bridge\n",
        ),
        (
            "compose.yaml",
            "runner",
            "  runner:\n    driver: bridge\n    external: true\n",
        ),
        (
            "compose.yaml",
            "edge",
            "  edge:\n    driver: host\n    external: false\n",
        ),
        (
            "compose.rootless.yaml",
            "engine",
            "  engine:\n    driver: bridge\n    external: true\n    internal: true\n",
        ),
    ],
)
def test_semantic_source_contract_rejects_unowned_or_nonbridge_network_maps(
    filename: str,
    network_name: str,
    replacement: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    _copy_source_contract(tmp_path)
    source_path = tmp_path / filename
    source = source_path.read_text(encoding="utf-8")
    pattern = rf"(?m)^  {re.escape(network_name)}:\n(?:    [^\n]*\n)*"
    mutated, count = re.subn(pattern, replacement, source, count=1)
    assert count == 1
    source_path.write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(contract, "ROOT", tmp_path)

    with pytest.raises(ValueError, match=r"network|bridge|external|owned"):
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
            expected_sandbox_network=DEFAULT_SANDBOX_NETWORK,
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
    monkeypatch.setenv("SANDBOX_NETWORK", UNIQUE_SANDBOX_NETWORK)
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
    expected_env["SANDBOX_NETWORK"] = UNIQUE_SANDBOX_NETWORK
    if dev and inherited_app_env is not None:
        expected_env["APP_ENV"] = inherited_app_env
    assert observed["render"] == (mode, dev, expected_env)
    assert observed["assert"] == (
        rendered,
        {
            "mode": mode,
            "dev": dev,
            "expected_app_env": expected_app_env,
            "expected_sandbox_network": UNIQUE_SANDBOX_NETWORK,
            "expected_rootful_gid": ROOTFUL_TEST_GID if mode == "rootful" else None,
            "expected_socket_source": (
                "/verified/docker.sock" if mode == "rootful" else ROOTLESS_SOCKET
            ),
        },
    )


def test_cli_uses_an_explicit_default_sandbox_network_from_a_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _load_contract()
    observed: dict[str, Any] = {}
    rendered: dict[str, Any] = {"services": {}}
    monkeypatch.delenv("SANDBOX_NETWORK", raising=False)
    monkeypatch.setenv("PHASE10_ROOTLESS_DOCKER_SOCKET", ROOTLESS_SOCKET)

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

    assert contract.main(["--mode", "rootless"]) == 0
    assert observed["render"] == (
        "rootless",
        False,
        {
            "PHASE10_ROOTLESS_DOCKER_SOCKET": ROOTLESS_SOCKET,
            "SANDBOX_NETWORK": DEFAULT_SANDBOX_NETWORK,
        },
    )
    assert observed["assert"][1]["expected_sandbox_network"] == DEFAULT_SANDBOX_NETWORK


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


async def test_product_removal_waiter_never_converts_a_survivor_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    state = {"identifiers": ["phase10-survivor"]}
    clock = iter((0.0, 31.0))

    def fake_docker(*args: str) -> str:
        if args[:2] == ("ps", "-aq"):
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f"):
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    monkeypatch.setattr(security, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(BaseException, match=r"survived cancellation"):
        await security._wait_for_job_container_removal("phase10-job")

    assert state["identifiers"] == ["phase10-survivor"]


async def test_product_removal_waiter_accepts_final_deadline_race_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    clock = iter((0.0, 31.0))

    def fake_docker(*args: str) -> str:
        assert args == (
            "ps",
            "-aq",
            "--filter",
            "label=jhin.sandbox.job=phase10-job",
        )
        return ""

    monkeypatch.setattr(security, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(security, "_docker", fake_docker)

    await security._wait_for_job_container_removal("phase10-job")


async def test_emergency_removal_force_removes_and_verifies_the_exact_job_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-emergency-job"
    state = {"identifiers": ["phase10-survivor"]}
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("ps", "-aq"):
            assert args[2:] == ("--filter", f"label=jhin.sandbox.job={job_id}")
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f"):
            assert args[2] == "phase10-survivor"
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    monkeypatch.setattr(security, "_docker", fake_docker)
    emergency_remove = getattr(
        security,
        "_emergency_force_remove_job_container",
        None,
    )
    assert callable(emergency_remove), "a separate emergency removal helper is required"

    await emergency_remove(job_id)

    assert state["identifiers"] == []
    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("rm", "-f", "phase10-survivor"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_emergency_removal_cleans_every_duplicate_before_raising_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-duplicate-job"
    state = {"identifiers": ["phase10-first", "phase10-second"]}
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("ps", "-aq"):
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f"):
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(AssertionError, match=r"matched multiple containers"):
        await security._emergency_force_remove_job_container(job_id)

    assert state["identifiers"] == []
    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("rm", "-f", "phase10-first"),
        ("rm", "-f", "phase10-second"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_emergency_removal_continues_after_first_rm_failure_and_retains_all_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-rm-failure-job"
    state = {"identifiers": ["phase10-first", "phase10-second"]}
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("ps", "-aq"):
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f") and args[2] == "phase10-first":
            raise SystemExit("first force removal failed")
        if args[:2] == ("rm", "-f"):
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(BaseExceptionGroup) as captured:
        await security._emergency_force_remove_job_container(job_id)

    assert [str(error) for error in captured.value.exceptions] == [
        "job label matched multiple containers: ['phase10-first', 'phase10-second']",
        "first force removal failed",
        "job phase10-rm-failure-job survived emergency force removal: ['phase10-first']",
    ]
    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("rm", "-f", "phase10-first"),
        ("rm", "-f", "phase10-second"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_emergency_removal_retains_a_final_query_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-surviving-job"
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("ps", "-aq"):
            return "phase10-survivor"
        if args[:2] == ("rm", "-f"):
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(AssertionError, match=r"survived emergency force removal"):
        await security._emergency_force_remove_job_container(job_id)

    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("rm", "-f", "phase10-survivor"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_emergency_removal_rechecks_after_initial_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-query-failure-job"
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("initial label query failed")
        assert args[:2] == ("ps", "-aq")
        return ""

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(RuntimeError, match=r"initial label query failed"):
        await security._emergency_force_remove_job_container(job_id)

    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_emergency_removal_retains_rm_and_final_query_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-final-query-failure-job"
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if len(calls) == 1:
            return "phase10-survivor"
        if args[:2] == ("rm", "-f"):
            raise RuntimeError("force removal failed")
        raise SystemExit("final label query failed")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(BaseExceptionGroup) as captured:
        await security._emergency_force_remove_job_container(job_id)

    assert [str(error) for error in captured.value.exceptions] == [
        "force removal failed",
        "final label query failed",
    ]
    assert calls == [
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
        ("rm", "-f", "phase10-survivor"),
        ("ps", "-aq", "--filter", f"label=jhin.sandbox.job={job_id}"),
    ]


async def test_live_job_cleanup_attempts_wait_and_removal_after_cancel_raises() -> None:
    from tests.integration.test_phase6_security import cleanup_live_job

    events: list[str] = []

    async def cancel() -> SimpleNamespace:
        events.append("cancel")
        raise RuntimeError("cancel transport failed")

    async def wait_terminal() -> dict[str, Any]:
        events.append("wait")
        return {"status": "cancelled"}

    async def remove_container() -> None:
        events.append("remove")

    with pytest.raises(RuntimeError, match=r"cancel transport failed"):
        await cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=remove_container,
        )

    assert events == ["cancel", "wait", "remove"]


@pytest.mark.parametrize("failure_kind", ["pytest", "system-exit"])
async def test_live_job_cleanup_base_exception_from_wait_cannot_skip_removal(
    failure_kind: str,
) -> None:
    from tests.integration.test_phase6_security import cleanup_live_job

    events: list[str] = []

    async def cancel() -> SimpleNamespace:
        events.append("cancel")
        return SimpleNamespace(status_code=200, text="cancelled")

    async def wait_terminal() -> dict[str, Any]:
        events.append("wait")
        if failure_kind == "pytest":
            pytest.fail("terminal wait failed")
        raise SystemExit("terminal wait exited")

    async def remove_container() -> None:
        events.append("remove")

    with pytest.raises(BaseException, match=r"terminal wait"):
        await cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=remove_container,
        )

    assert events == ["cancel", "wait", "remove"]


async def test_live_job_cleanup_asserts_cancel_result_only_after_removal() -> None:
    from tests.integration.test_phase6_security import cleanup_live_job

    events: list[str] = []

    async def cancel() -> SimpleNamespace:
        events.append("cancel")
        return SimpleNamespace(status_code=503, text="cancel denied")

    async def wait_terminal() -> dict[str, Any]:
        events.append("wait")
        return {"status": "running"}

    async def remove_container() -> None:
        events.append("remove")

    with pytest.raises(AssertionError, match=r"cancel: 503"):
        await cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=remove_container,
        )

    assert events == ["cancel", "wait", "remove"]


async def test_live_job_cleanup_force_removes_survivor_but_retains_product_failure() -> None:
    from tests.integration.test_phase6_security import cleanup_live_job

    state = {"survivor": True}

    async def cancel() -> SimpleNamespace:
        return SimpleNamespace(status_code=200, text="cancelled")

    async def wait_terminal() -> dict[str, Any]:
        return {"status": "cancelled"}

    async def prove_product_removal() -> None:
        pytest.fail("product removal invariant failed")

    async def emergency_force_remove() -> None:
        state["survivor"] = False

    with pytest.raises(BaseException, match=r"product removal invariant failed"):
        await cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=prove_product_removal,
            emergency_remove_container=emergency_force_remove,
        )

    assert state["survivor"] is False


async def test_live_job_cleanup_retains_product_and_emergency_failures() -> None:
    from tests.integration.test_phase6_security import cleanup_live_job

    async def cancel() -> SimpleNamespace:
        return SimpleNamespace(status_code=200, text="cancelled")

    async def wait_terminal() -> dict[str, Any]:
        return {"status": "cancelled"}

    async def prove_product_removal() -> None:
        pytest.fail("product removal invariant failed")

    async def emergency_force_remove() -> None:
        raise SystemExit("emergency force removal failed")

    with pytest.raises(BaseExceptionGroup) as captured:
        await cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=prove_product_removal,
            emergency_remove_container=emergency_force_remove,
        )

    messages = {str(error) for error in captured.value.exceptions}
    assert messages == {
        "product removal invariant failed",
        "emergency force removal failed",
    }


async def test_live_job_cleanup_retains_product_and_duplicate_emergency_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-outer-cleanup-job"
    state = {"identifiers": ["phase10-first", "phase10-second"]}

    def fake_docker(*args: str) -> str:
        if args[:2] == ("ps", "-aq"):
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f"):
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    async def cancel() -> SimpleNamespace:
        return SimpleNamespace(status_code=200, text="cancelled")

    async def wait_terminal() -> dict[str, Any]:
        return {"status": "cancelled"}

    async def prove_product_removal() -> None:
        pytest.fail("product removal invariant failed")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(BaseExceptionGroup) as captured:
        await security.cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=prove_product_removal,
            emergency_remove_container=lambda: security._emergency_force_remove_job_container(
                job_id
            ),
        )

    assert [str(error) for error in captured.value.exceptions] == [
        "product removal invariant failed",
        "job label matched multiple containers: ['phase10-first', 'phase10-second']",
    ]
    assert state["identifiers"] == []


async def test_live_job_cleanup_retains_nested_emergency_failure_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.integration.test_phase6_security as security

    job_id = "phase10-outer-group-job"
    state = {"identifiers": ["phase10-first", "phase10-second"]}

    def fake_docker(*args: str) -> str:
        if args[:2] == ("ps", "-aq"):
            return "\n".join(state["identifiers"])
        if args[:2] == ("rm", "-f") and args[2] == "phase10-first":
            raise RuntimeError("first force removal failed")
        if args[:2] == ("rm", "-f"):
            state["identifiers"].remove(args[2])
            return args[2]
        raise AssertionError(f"unexpected Docker argv: {args}")

    async def cancel() -> SimpleNamespace:
        return SimpleNamespace(status_code=200, text="cancelled")

    async def wait_terminal() -> dict[str, Any]:
        return {"status": "cancelled"}

    async def prove_product_removal() -> None:
        pytest.fail("product removal invariant failed")

    monkeypatch.setattr(security, "_docker", fake_docker)

    with pytest.raises(BaseExceptionGroup) as captured:
        await security.cleanup_live_job(
            cancel=cancel,
            wait_terminal=wait_terminal,
            remove_container=prove_product_removal,
            emergency_remove_container=lambda: security._emergency_force_remove_job_container(
                job_id
            ),
        )

    assert str(captured.value.exceptions[0]) == "product removal invariant failed"
    emergency_group = captured.value.exceptions[1]
    assert isinstance(emergency_group, BaseExceptionGroup)
    assert [str(error) for error in emergency_group.exceptions] == [
        "job label matched multiple containers: ['phase10-first', 'phase10-second']",
        "first force removal failed",
        "job phase10-outer-group-job survived emergency force removal: ['phase10-first']",
    ]
    assert state["identifiers"] == ["phase10-first"]
