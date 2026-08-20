#!/usr/bin/env python3
"""Render and exhaustively assert the Phase 10 Compose authority boundary."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
ComposeMode = Literal["rootful", "rootless"]
_ROOTLESS_TRANSPORT_URL = "http://rootless-docker-transport:2375"
_RUNNER_IMAGE = "jhin-sandbox-runner:local"
_DEFAULT_SANDBOX_NETWORK = "jhin_sandbox"
_SANDBOX_NETWORK_INTERPOLATION = "${SANDBOX_NETWORK:-jhin_sandbox}"
_SANDBOX_NETWORK_MAX_LENGTH = 63
_SANDBOX_NETWORK_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*\Z")
_RESERVED_SANDBOX_NETWORKS = {
    "bridge",
    "default",
    "engine",
    "host",
    "none",
    "runner",
}
_BOUNDARY_NETWORKS = {"control", "data", "edge", "engine", "runner", "sandbox"}
_RUNNER_HEALTHCHECK = {
    "interval": "10s",
    "retries": 5,
    "start_period": "15s",
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request, sys\n"
        'sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8085/health", '
        "timeout=3).status == 200 else 1)\n",
    ],
    "timeout": "5s",
}
_ADAPTER_HEALTHCHECK = {
    "interval": "2s",
    "retries": 15,
    "test": [
        "CMD",
        "python",
        "-c",
        "import urllib.request;response=urllib.request.urlopen("
        "'http://127.0.0.1:2375/_ping',timeout=2);body=response.read();"
        "raise SystemExit(0 if response.status == 200 and body == b'OK' else 1)",
    ],
    "timeout": "2s",
}
_RUNNER_BASE_ENVIRONMENT_KEYS = {
    "APP_ENV",
    "LOG_LEVEL",
    "SANDBOX_DEFAULT_IMAGE",
    "SANDBOX_NETWORK",
    "SANDBOX_RUNNER_TOKEN",
}
_DEV_HTTP_ORIGINS = (
    "http://fake-github:8080,http://fake-linear:8080,"
    "http://fake-vercel:8080,http://fake-supabase:8080"
)
_DEV_DB_HOSTS = "fake-supabase-db:5432"
_CORE_DEPENDENCIES = {
    "nats": {"condition": "service_healthy", "required": True},
    "postgres": {"condition": "service_healthy", "required": True},
    "temporal": {"condition": "service_healthy", "required": True},
}
_CRASH_KEYS = {
    "JHIN_TEST_CRASH_BARRIER_DIR",
    "JHIN_TEST_CRASH_BARRIER_NAME",
    "JHIN_TEST_CRASH_BARRIER_MATCH",
}
_MODE_SENSITIVE_PREFIXES = (
    "COMPOSE_",
    "PHASE10_",
    "SANDBOX_",
    "JHIN_TEST_CRASH_BARRIER_",
)


class StatResult(Protocol):
    @property
    def st_mode(self) -> int: ...

    @property
    def st_uid(self) -> int: ...

    @property
    def st_gid(self) -> int: ...


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that rejects direct and ambiguous merged duplicate keys."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise ValueError("YAML mapping node is required")

        direct_key_nodes: set[int] = set()
        direct_keys: set[Any] = set()
        merge_keys = 0
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                merge_keys += 1
                if merge_keys > 1:
                    raise ValueError("duplicate YAML merge key is forbidden")
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in direct_keys
                direct_keys.add(key)
            except TypeError as exc:
                raise ValueError("YAML mapping keys must be hashable") from exc
            if duplicate:
                raise ValueError(f"duplicate YAML mapping key: {key!r}")
            direct_key_nodes.add(id(key_node))

        self.flatten_mapping(node)
        seen_origins: dict[Any, list[bool]] = {}
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            is_direct = id(key_node) in direct_key_nodes
            try:
                origins = seen_origins.setdefault(key, [])
            except TypeError as exc:
                raise ValueError("YAML mapping keys must be hashable") from exc
            if origins:
                safe_direct_override = is_direct and origins == [False]
                if not safe_direct_override:
                    raise ValueError(f"duplicate YAML mapping key after merge: {key!r}")
            origins.append(is_direct)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _path_lstat(path: Path) -> StatResult:
    return path.lstat()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_mapping(value: object, message: str) -> dict[str, Any]:
    _require(isinstance(value, dict), message)
    return cast(dict[str, Any], value)


def compose_files(mode: str, *, dev: bool = False) -> tuple[str, ...]:
    """Return the only file vector accepted by this assertion authority."""
    _require(mode in {"rootful", "rootless"}, "mode must be rootful or rootless")
    selected = cast(ComposeMode, mode)
    if dev:
        return ("compose.yaml", "compose.dev.yaml", f"compose.{selected}.yaml")
    return ("compose.yaml", f"compose.{selected}.yaml")


def _clean_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Make Compose independent from inherited profiles, files, modes, and .env."""
    cleaned = {
        key: value
        for key, value in os.environ.items()
        if key != "APP_ENV" and not key.startswith(_MODE_SENSITIVE_PREFIXES)
    }
    cleaned["COMPOSE_DISABLE_ENV_FILE"] = "1"
    cleaned.update(overrides or {})
    return cleaned


def render_compose(
    mode: str,
    *,
    dev: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render one exact authority vector from the repository root."""
    render_env = dict(env or {})
    if dev:
        render_env.setdefault("APP_ENV", "dev")
    command = ["docker", "compose"]
    for filename in compose_files(mode, dev=dev):
        command.extend(("-f", filename))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_clean_environment(render_env),
        check=True,
        text=True,
        capture_output=True,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


def validate_rootful_socket(
    socket_path: str,
    configured_gid: str,
    *,
    _lstat: Callable[[Path], StatResult] = _path_lstat,
) -> tuple[str, int]:
    """Validate rootful authority with lstat and never mutate the host path."""
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("rootful Docker socket path must be absolute")
    try:
        gid = int(configured_gid)
    except ValueError as exc:
        raise ValueError("SANDBOX_DOCKER_GID must be a positive integer") from exc
    if gid <= 0:
        raise ValueError("SANDBOX_DOCKER_GID must be a positive integer")
    try:
        info = _lstat(path)
    except OSError as exc:
        raise ValueError("cannot inspect rootful Docker socket") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("rootful Docker socket must not be a symlink")
    if not stat.S_ISSOCK(info.st_mode):
        raise ValueError("rootful Docker authority must be a Unix socket")
    if info.st_uid != 0:
        raise ValueError("rootful Docker socket must be owned by UID 0")
    if info.st_gid != gid:
        raise ValueError("rootful Docker socket group does not match SANDBOX_DOCKER_GID")
    return str(path), gid


def validate_sandbox_network(value: str) -> str:
    """Reject ambiguous or unsafe job-network authority names.

    The 63-character cap is deliberately conservative for Task 10's
    project-scoped Docker bridge names and keeps them DNS-label sized.
    """
    stripped = value.strip()
    _require(bool(stripped), "SANDBOX_NETWORK must be nonempty")
    _require(value == stripped, "SANDBOX_NETWORK must not contain surrounding whitespace")
    normalized = stripped.casefold()
    _require(
        normalized not in _RESERVED_SANDBOX_NETWORKS,
        "SANDBOX_NETWORK must not use a Docker reserved network mode",
    )
    _require(
        not re.sub(r"\s+", "", normalized).startswith("container:"),
        "SANDBOX_NETWORK must not use Docker container network mode",
    )
    _require(
        len(normalized) <= _SANDBOX_NETWORK_MAX_LENGTH,
        f"SANDBOX_NETWORK must be at most {_SANDBOX_NETWORK_MAX_LENGTH} characters",
    )
    _require(
        _SANDBOX_NETWORK_PATTERN.fullmatch(stripped) is not None,
        "SANDBOX_NETWORK must match [a-z0-9][a-z0-9_.-]*",
    )
    return value


def _network_names(service: Mapping[str, Any]) -> set[str]:
    networks = service.get("networks", {})
    _require(isinstance(networks, dict), "rendered service networks must be a mapping")
    return set(networks)


def _secret_sources(service: Mapping[str, Any]) -> set[str]:
    secrets = service.get("secrets", [])
    _require(isinstance(secrets, list), "rendered service secrets must be a list")
    return {
        str(secret["source"])
        for secret in secrets
        if isinstance(secret, dict) and "source" in secret
    }


def _load_yaml_mapping(filename: str) -> dict[str, Any]:
    try:
        loaded: object = yaml.load(
            (ROOT / filename).read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{filename} is not unambiguous safe YAML: {exc}") from exc
    _require(isinstance(loaded, dict), f"{filename} root must be a mapping")
    return cast(dict[str, Any], loaded)


def _require_source_socket_bind(
    overlay: Mapping[str, Any],
    *,
    filename: str,
    service_name: str,
    source: str,
    target: str,
) -> None:
    services = _require_mapping(overlay.get("services"), f"{filename} services must be a mapping")
    service = _require_mapping(
        services.get(service_name),
        f"{filename} {service_name} must be a mapping",
    )
    volumes = service.get("volumes")
    _require(
        isinstance(volumes, list) and len(volumes) == 1,
        f"{filename} {service_name} must have exactly one socket bind volume",
    )
    volume_values = cast(list[object], volumes)
    volume = _require_mapping(volume_values[0], f"{filename} socket volume must be a mapping")
    _require(
        set(volume) == {"bind", "source", "target", "type"},
        f"{filename} socket bind fields drifted",
    )
    _require(volume.get("type") == "bind", f"{filename} socket volume must be a bind")
    _require(volume.get("source") == source, f"{filename} socket bind source drifted")
    _require(volume.get("target") == target, f"{filename} socket bind target drifted")
    bind = volume.get("bind")
    _require(
        isinstance(bind, dict)
        and set(bind) == {"create_host_path"}
        and bind.get("create_host_path") is False,
        f"{filename} socket bind create_host_path must be exactly false",
    )


def assert_source_contract() -> None:
    """Assert semantic source-only safety that Compose normalizes out of renders."""
    base = _load_yaml_mapping("compose.yaml")
    rootful = _load_yaml_mapping("compose.rootful.yaml")
    rootless = _load_yaml_mapping("compose.rootless.yaml")

    services = _require_mapping(base.get("services"), "compose.yaml services must be a mapping")
    runner = _require_mapping(
        services.get("sandbox-runner"),
        "compose.yaml sandbox-runner must be a mapping",
    )
    runner_environment = runner.get("environment")
    _require(
        isinstance(runner_environment, dict)
        and runner_environment.get("SANDBOX_NETWORK") == _SANDBOX_NETWORK_INTERPOLATION,
        "compose.yaml runner SANDBOX_NETWORK interpolation drifted",
    )
    networks = _require_mapping(base.get("networks"), "compose.yaml networks must be a mapping")
    _require(
        networks
        == {
            "edge": {"driver": "bridge", "external": False},
            "control": {"driver": "bridge", "external": False},
            "data": {"driver": "bridge", "external": False},
            "runner": {"driver": "bridge", "external": False},
            "sandbox": {
                "name": _SANDBOX_NETWORK_INTERPOLATION,
                "driver": "bridge",
                "external": False,
            },
        },
        "compose.yaml boundary networks must be exact owned nonexternal bridges",
    )
    rootless_networks = _require_mapping(
        rootless.get("networks"),
        "compose.rootless.yaml networks must be a mapping",
    )
    _require(
        rootless_networks
        == {
            "engine": {
                "driver": "bridge",
                "external": False,
                "internal": True,
            }
        },
        "compose.rootless.yaml engine must be an exact owned internal bridge",
    )

    _require_source_socket_bind(
        rootful,
        filename="compose.rootful.yaml",
        service_name="sandbox-runner",
        source="${SANDBOX_DOCKER_SOCKET_HOST:?set verified absolute Docker socket}",
        target="/run/jhin/docker.sock",
    )
    _require_source_socket_bind(
        rootless,
        filename="compose.rootless.yaml",
        service_name="rootless-docker-transport",
        source="${PHASE10_ROOTLESS_DOCKER_SOCKET:?set the verified rootless socket}",
        target="/run/host/docker.sock",
    )


def _assert_common_workers(
    services: Mapping[str, Any],
    *,
    dev: bool,
    expected_app_env: str,
    expected_sandbox_network: str,
) -> None:
    agent = cast(dict[str, Any], services["agent-worker"])
    tool = cast(dict[str, Any], services["tool-worker"])
    runner = cast(dict[str, Any], services["sandbox-runner"])

    for service_name in (
        "api",
        "agent-worker",
        "tool-worker",
        "event-worker",
        "workflow-worker",
        "sandbox-runner",
    ):
        environment = cast(dict[str, Any], services[service_name]["environment"])
        service_app_env = "dev" if dev and service_name == "api" else expected_app_env
        _require(
            environment.get("APP_ENV") == service_app_env,
            f"{service_name} APP_ENV drifted",
        )

    _require(agent["command"] == ["jhin-agent-worker"], "agent command drifted")
    _require(tool["command"] == ["jhin-tool-worker"], "tool command drifted")
    _require(_network_names(agent) == {"control", "data"}, "agent network boundary drifted")
    _require(
        _network_names(tool) == {"control", "data", "runner"},
        "tool network boundary drifted",
    )
    _require(agent.get("depends_on") == _CORE_DEPENDENCIES, "agent dependencies drifted")
    _require(tool.get("depends_on") == _CORE_DEPENDENCIES, "tool dependencies drifted")
    _require(
        agent["healthcheck"]["test"] == ["CMD", "jhin-temporal-poller-check", "jhin-agent-queue"],
        "agent queue health command drifted",
    )
    _require(
        tool["healthcheck"]["test"] == ["CMD", "jhin-temporal-poller-check", "jhin-tool-queue"],
        "tool queue health command drifted",
    )

    agent_environment = cast(dict[str, Any], agent["environment"])
    tool_environment = cast(dict[str, Any], tool["environment"])
    for key in ("SANDBOX_RUNNER_URL", "SANDBOX_RUNNER_TOKEN", "SANDBOX_DEFAULT_IMAGE"):
        _require(key not in agent_environment, f"agent retained {key}")
    _require(
        tool_environment.get("SANDBOX_RUNNER_URL") == "http://sandbox-runner:8085",
        "tool runner URL drifted",
    )
    _require(
        tool_environment.get("SANDBOX_RUNNER_TOKEN") == "dev-sandbox-runner-token",
        "tool runner token default drifted",
    )
    _require(
        tool_environment.get("SANDBOX_DEFAULT_IMAGE") == "jhin-sandbox:latest",
        "tool default job image drifted",
    )
    _require(
        not any(
            marker in key.upper()
            for key in tool_environment
            for marker in ("MODEL", "PROVIDER", "PROMPT")
        ),
        "tool worker received model/provider/prompt authority",
    )

    runner_environment = cast(dict[str, Any], runner.get("environment", {}))
    runner_token = runner_environment.get("SANDBOX_RUNNER_TOKEN")
    _require(
        isinstance(runner_token, str) and bool(runner_token),
        "runner token must be nonempty",
    )
    _require(
        runner_token == tool_environment.get("SANDBOX_RUNNER_TOKEN"),
        "runner and tool-worker tokens differ",
    )
    _require(
        runner_environment.get("SANDBOX_DEFAULT_IMAGE")
        == tool_environment.get("SANDBOX_DEFAULT_IMAGE"),
        "runner and tool-worker default images differ",
    )
    _require(
        runner_environment.get("SANDBOX_NETWORK") == expected_sandbox_network,
        "runner SANDBOX_NETWORK differs from the expected sandbox network",
    )

    _require(runner.get("user") == "10001:10001", "runner user must be 10001:10001")
    _require(runner.get("privileged", False) is False, "runner must not be privileged")
    _require(runner.get("cap_drop") == ["ALL"], "runner must drop every capability")
    _require(
        runner.get("security_opt") == ["no-new-privileges:true"],
        "runner no-new-privileges drifted",
    )
    _require(runner.get("restart") == "unless-stopped", "runner restart policy drifted")
    _require(runner.get("image") == _RUNNER_IMAGE, "runner image tag drifted")
    _require(runner.get("pull_policy") == "build", "runner must build locally")
    _require(runner.get("healthcheck") == _RUNNER_HEALTHCHECK, "runner health map drifted")

    protected = {"api", "agent-worker", "tool-worker"}
    recipients = {
        name for name, service in services.items() if _secret_sources(cast(dict[str, Any], service))
    }
    _require(recipients == protected, "master-key secret recipients drifted")
    for name in protected:
        _require(
            _secret_sources(cast(dict[str, Any], services[name])) == {"jhin_master_key"},
            f"{name} secret set drifted",
        )


def _assert_sandbox_network_contract(
    config: Mapping[str, Any],
    services: Mapping[str, Any],
    *,
    dev: bool,
    expected_sandbox_network: str,
) -> None:
    networks = _require_mapping(config.get("networks"), "rendered networks must be a mapping")

    sandbox_users = {
        name for name, service in services.items() if "sandbox" in _network_names(service)
    }
    expected_users = {"fake-github"} if dev else set()
    _require(
        sandbox_users == expected_users,
        "logical sandbox network users drifted",
    )

    physical_names: dict[str, str] = {}
    for logical_name, network_value in networks.items():
        network = _require_mapping(
            network_value,
            f"network {logical_name} must be a mapping",
        )
        if logical_name in _BOUNDARY_NETWORKS:
            _require(
                network.get("driver") == "bridge",
                f"network {logical_name} must use the bridge driver",
            )
            _require(
                network.get("external", False) is False,
                f"network {logical_name} must be nonexternal and Compose-owned",
            )
        physical_name = network.get("name")
        _require(
            isinstance(physical_name, str) and bool(physical_name),
            f"network {logical_name} physical name is missing",
        )
        physical_name = cast(str, physical_name)
        if logical_name == "sandbox":
            continue
        _require(
            physical_name not in physical_names,
            f"rendered physical network name collision: {physical_name}",
        )
        physical_names[physical_name] = str(logical_name)

    _require(
        expected_sandbox_network not in physical_names,
        "expected sandbox network collides with rendered physical authority network",
    )

    has_rendered_sandbox = "sandbox" in networks
    rendered_sandbox = networks.get("sandbox")
    if dev:
        _require(
            isinstance(rendered_sandbox, dict),
            "development sandbox network declaration is missing",
        )
    if has_rendered_sandbox:
        _require(
            isinstance(rendered_sandbox, dict)
            and rendered_sandbox.get("name") == expected_sandbox_network,
            "rendered sandbox network name differs from expected",
        )


def _assert_dev_contract(services: Mapping[str, Any], *, dev: bool) -> None:
    allowlist_recipients = {
        name
        for name, service in services.items()
        if "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
        in cast(dict[str, Any], service).get("environment", {})
    }
    database_recipients = {
        name
        for name, service in services.items()
        if "JHIN_CONNECTOR_ALLOWED_DB_HOSTS" in cast(dict[str, Any], service).get("environment", {})
    }
    if not dev:
        _require(not allowlist_recipients, "HTTP connector allowlist leaked to production")
        _require(not database_recipients, "DB connector allowlist leaked to production")
        serialized = json.dumps(services, sort_keys=True)
        _require("JHIN_TEST_CRASH_BARRIER_" not in serialized, "crash controls leaked")
        _require("/run/jhin/test-barriers" not in serialized, "crash mount leaked")
        for fake in (
            "fake-github",
            "fake-linear",
            "fake-vercel",
            "fake-supabase",
            "fake-provider",
        ):
            _require(fake not in services, f"{fake} leaked to production")
        return

    _require(allowlist_recipients == {"api", "tool-worker"}, "HTTP allowlist owners drifted")
    _require(database_recipients == {"api", "tool-worker"}, "DB allowlist owners drifted")
    for name in allowlist_recipients:
        _require(
            services[name]["environment"]["JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"]
            == _DEV_HTTP_ORIGINS,
            f"{name} HTTP allowlist drifted",
        )
    for name in database_recipients:
        _require(
            services[name]["environment"]["JHIN_CONNECTOR_ALLOWED_DB_HOSTS"] == _DEV_DB_HOSTS,
            f"{name} DB allowlist drifted",
        )
    for name in ("agent-worker", "tool-worker"):
        environment = cast(dict[str, Any], services[name]["environment"])
        _require(environment.keys() >= _CRASH_KEYS, f"{name} crash controls missing")
        _require(
            environment["JHIN_TEST_CRASH_BARRIER_DIR"] == "",
            f"{name} crash directory default drifted",
        )
        _require(
            "JHIN_TEST_CRASH_BARRIER_HOST_DIR" not in environment,
            f"{name} received the host barrier path",
        )
        volumes = services[name].get("volumes", [])
        _require(
            len(volumes) == 1
            and volumes[0]["target"] == "/run/jhin/test-barriers"
            and volumes[0]["source"] == "/tmp/jhin-disabled-barriers",
            f"{name} crash mount drifted",
        )
    for name in ("fake-github", "fake-linear", "fake-vercel", "fake-supabase"):
        _require(
            services[name]["build"]["args"]["SERVICE_PACKAGE"] == "jhin-tool-worker",
            f"{name} must use the tool-worker image",
        )
    _require(
        services["fake-provider"]["build"]["args"]["SERVICE_PACKAGE"] == "jhin-agent-worker",
        "fake provider must use the agent-worker image",
    )


def _assert_ports(services: Mapping[str, Any], *, mode: ComposeMode, dev: bool) -> None:
    boundary_services = ["agent-worker", "tool-worker", "sandbox-runner"]
    if mode == "rootless":
        boundary_services.append("rootless-docker-transport")
    for name in boundary_services:
        ports = cast(dict[str, Any], services[name]).get("ports")
        if dev and name == "sandbox-runner":
            _require(
                ports
                == [
                    {
                        "host_ip": "127.0.0.1",
                        "mode": "ingress",
                        "protocol": "tcp",
                        "published": "8093",
                        "target": 8085,
                    }
                ],
                "dev runner port must be the sole loopback boundary port",
            )
        else:
            _require(not ports, f"{name} unexpectedly publishes a port")


def _assert_rootless(
    config: Mapping[str, Any],
    services: Mapping[str, Any],
    *,
    expected_app_env: str,
    expected_socket_source: str | None,
) -> None:
    runner = cast(dict[str, Any], services["sandbox-runner"])
    adapter = cast(dict[str, Any], services.get("rootless-docker-transport"))
    _require(adapter is not None, "rootless authority overlay is missing")
    runner_environment = cast(dict[str, Any], runner["environment"])
    expected_runner_keys = _RUNNER_BASE_ENVIRONMENT_KEYS | {
        "SANDBOX_DOCKER_MODE",
        "SANDBOX_DOCKER_TRANSPORT_URL",
    }
    _require(
        set(runner_environment) == expected_runner_keys,
        "rootless runner environment key set drifted",
    )
    _require(runner.get("group_add", []) == [], "rootless runner must have no group_add")
    _require(runner.get("volumes", []) == [], "rootless runner must have no socket volume")
    _require(_network_names(runner) == {"runner", "engine"}, "rootless runner networks drifted")
    _require(runner_environment.get("SANDBOX_DOCKER_MODE") == "rootless", "mode drifted")
    _require(
        runner_environment.get("SANDBOX_DOCKER_TRANSPORT_URL") == _ROOTLESS_TRANSPORT_URL,
        "rootless transport URL drifted",
    )
    for key in ("SANDBOX_DOCKER_GID", "SANDBOX_DOCKER_SOCKET"):
        _require(key not in runner_environment, f"rootless runner received {key}")
    _require(
        runner.get("depends_on")
        == {
            "rootless-docker-transport": {
                "condition": "service_healthy",
                "restart": True,
                "required": True,
            }
        },
        "rootless startup dependency drifted",
    )

    _require(adapter.get("user") == "0:0", "adapter user must be 0:0")
    _require(adapter.get("group_add", []) == [], "adapter must have no group_add authority")
    _require(adapter.get("privileged", False) is False, "adapter must not be privileged")
    _require(adapter.get("cap_drop") == ["ALL"], "adapter must drop every capability")
    _require(
        adapter.get("security_opt") == ["no-new-privileges:true"],
        "adapter no-new-privileges drifted",
    )
    _require(adapter.get("read_only") is True, "adapter must be read-only")
    _require(adapter.get("tmpfs") == ["/tmp"], "adapter tmpfs drifted")
    _require(_network_names(adapter) == {"engine"}, "adapter must be engine-only")
    _require(adapter.get("restart") == "unless-stopped", "adapter restart policy drifted")
    _require(adapter.get("image") == runner.get("image"), "adapter/runner images differ")
    _require(adapter.get("pull_policy") == "never", "adapter must never pull")
    adapter_environment = adapter.get("environment", {})
    _require(
        isinstance(adapter_environment, dict)
        and set(adapter_environment) == {"APP_ENV", "LOG_LEVEL"},
        "adapter environment key set drifted",
    )
    _require(
        adapter_environment.get("APP_ENV") == expected_app_env,
        "adapter APP_ENV drifted",
    )
    _require(
        adapter.get("command") == ["python", "-m", "jhin_sandbox_runner.rootless_transport"],
        "adapter entrypoint drifted",
    )
    _require(adapter.get("healthcheck") == _ADAPTER_HEALTHCHECK, "adapter health map drifted")
    volumes = adapter.get("volumes", [])
    _require(len(volumes) == 1, "adapter must have one socket bind")
    _require(expected_socket_source is not None, "rootless expected socket source is required")
    volume = volumes[0]
    _require(
        volume
        == {
            "type": "bind",
            "source": expected_socket_source,
            "target": "/run/host/docker.sock",
            "bind": {},
        }
        and str(expected_socket_source).startswith("/"),
        "adapter socket source or canonical long bind drifted",
    )
    _require(config["networks"]["engine"].get("internal") is True, "engine must be internal")


def _assert_rootful(
    services: Mapping[str, Any],
    *,
    expected_gid: int | None,
    expected_socket_source: str | None,
) -> None:
    _require("rootless-docker-transport" not in services, "rootless adapter leaked to rootful")
    _require(expected_gid is not None and expected_gid > 0, "rootful expected GID is required")
    _require(expected_socket_source is not None, "rootful socket source is required")
    runner = cast(dict[str, Any], services["sandbox-runner"])
    environment = cast(dict[str, Any], runner["environment"])
    expected_runner_keys = _RUNNER_BASE_ENVIRONMENT_KEYS | {
        "SANDBOX_DOCKER_GID",
        "SANDBOX_DOCKER_MODE",
        "SANDBOX_DOCKER_SOCKET",
    }
    _require(
        set(environment) == expected_runner_keys,
        "rootful runner environment key set drifted",
    )
    _require(_network_names(runner) == {"runner"}, "rootful runner networks drifted")
    _require(runner.get("group_add") == [str(expected_gid)], "rootful sole group drifted")
    _require(runner.get("depends_on", {}) == {}, "rootful runner has a daemon dependency")
    _require(environment.get("SANDBOX_DOCKER_MODE") == "rootful", "mode drifted")
    _require(environment.get("SANDBOX_DOCKER_GID") == str(expected_gid), "GID drifted")
    _require(
        environment.get("SANDBOX_DOCKER_SOCKET") == "/run/jhin/docker.sock",
        "rootful container socket drifted",
    )
    _require(
        "SANDBOX_DOCKER_TRANSPORT_URL" not in environment,
        "rootful runner received transport URL",
    )
    _require(
        runner.get("volumes")
        == [
            {
                "type": "bind",
                "source": expected_socket_source,
                "target": "/run/jhin/docker.sock",
                "bind": {},
            }
        ],
        "rootful socket bind must be canonical long syntax",
    )


def assert_rendered_contract(
    config: dict[str, Any],
    *,
    mode: str,
    dev: bool,
    expected_app_env: str,
    expected_sandbox_network: str,
    expected_rootful_gid: int | None = None,
    expected_socket_source: str | None = None,
) -> None:
    """Assert the one exhaustive production/dev x rootful/rootless contract."""
    assert_source_contract()
    _require(mode in {"rootful", "rootless"}, "mode must be rootful or rootless")
    expected_sandbox_network = validate_sandbox_network(expected_sandbox_network)
    _require(
        expected_app_env in ({"dev", "test"} if dev else {"production"}),
        "expected APP_ENV does not match the selected production/dev contract",
    )
    selected = cast(ComposeMode, mode)
    services = cast(dict[str, Any], config.get("services", {}))
    for required in ("api", "agent-worker", "tool-worker", "sandbox-runner"):
        _require(required in services, f"required service missing: {required}")

    runner = cast(dict[str, Any], services["sandbox-runner"])
    runner_environment = cast(dict[str, Any], runner.get("environment", {}))
    has_adapter = "rootless-docker-transport" in services
    has_rootful_authority = any(
        key in runner_environment for key in ("SANDBOX_DOCKER_GID", "SANDBOX_DOCKER_SOCKET")
    ) or bool(runner.get("group_add"))
    if selected == "rootless":
        _require(has_adapter, "rootless authority overlay is missing")
        _require(not has_rootful_authority, "merged authority overlays are forbidden")
    else:
        _require(not has_adapter, "merged authority overlays are forbidden")
        _require(
            runner_environment.get("SANDBOX_DOCKER_MODE") == "rootful",
            "rootful authority overlay is missing",
        )

    _assert_common_workers(
        services,
        dev=dev,
        expected_app_env=expected_app_env,
        expected_sandbox_network=expected_sandbox_network,
    )
    _assert_dev_contract(services, dev=dev)
    _assert_sandbox_network_contract(
        config,
        services,
        dev=dev,
        expected_sandbox_network=expected_sandbox_network,
    )
    _assert_ports(services, mode=selected, dev=dev)
    if selected == "rootless":
        _assert_rootless(
            config,
            services,
            expected_app_env=expected_app_env,
            expected_socket_source=expected_socket_source,
        )
    else:
        _assert_rootful(
            services,
            expected_gid=expected_rootful_gid,
            expected_socket_source=expected_socket_source,
        )

    engine_users = {
        name for name, service in services.items() if "engine" in _network_names(service)
    }
    expected_engine_users = (
        {"sandbox-runner", "rootless-docker-transport"} if selected == "rootless" else set()
    )
    _require(engine_users == expected_engine_users, "engine network authority leaked")
    runner_users = {
        name for name, service in services.items() if "runner" in _network_names(service)
    }
    _require(
        runner_users == {"sandbox-runner", "tool-worker"},
        "runner network authority leaked",
    )
    _require(
        _network_names(cast(dict[str, Any], services["api"])) == {"control", "data", "edge"},
        "api network boundary drifted",
    )
    networks = config.get("networks", {})
    _require(isinstance(networks, dict), "rendered networks must be a mapping")
    _require("default" not in networks, "implicit default network is forbidden")
    _require(
        all(
            "default" not in _network_names(cast(dict[str, Any], service))
            for service in services.values()
        ),
        "service retained implicit default network authority",
    )
    _require(
        "engine" not in _network_names(cast(dict[str, Any], services["tool-worker"])),
        "tool worker reached the engine network",
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("rootful", "rootless"), required=True)
    parser.add_argument("--dev", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode = cast(ComposeMode, args.mode)
    explicit_env: dict[str, str] = {}
    expected_app_env = "production"
    expected_sandbox_network = validate_sandbox_network(
        os.environ.get("SANDBOX_NETWORK", _DEFAULT_SANDBOX_NETWORK)
    )
    explicit_env["SANDBOX_NETWORK"] = expected_sandbox_network
    expected_gid: int | None = None
    expected_socket: str | None = None
    if mode == "rootful":
        socket_value = os.environ.get("SANDBOX_DOCKER_SOCKET_HOST", "")
        gid_value = os.environ.get("SANDBOX_DOCKER_GID", "")
        expected_socket, expected_gid = validate_rootful_socket(socket_value, gid_value)
        explicit_env.update(
            {
                "SANDBOX_DOCKER_SOCKET_HOST": expected_socket,
                "SANDBOX_DOCKER_GID": str(expected_gid),
            }
        )
    else:
        rootless_socket = os.environ.get("PHASE10_ROOTLESS_DOCKER_SOCKET", "")
        if not rootless_socket:
            raise ValueError("PHASE10_ROOTLESS_DOCKER_SOCKET is required")
        explicit_env["PHASE10_ROOTLESS_DOCKER_SOCKET"] = rootless_socket
        expected_socket = rootless_socket
    if args.dev:
        expected_app_env = os.environ.get("APP_ENV", "dev")
        if "APP_ENV" in os.environ:
            explicit_env["APP_ENV"] = expected_app_env
    rendered = render_compose(mode, dev=bool(args.dev), env=explicit_env)
    assert_rendered_contract(
        rendered,
        mode=mode,
        dev=bool(args.dev),
        expected_app_env=expected_app_env,
        expected_sandbox_network=expected_sandbox_network,
        expected_rootful_gid=expected_gid,
        expected_socket_source=expected_socket,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
