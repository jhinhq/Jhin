#!/usr/bin/env python3
"""Render and exhaustively assert the Phase 10 Compose authority boundary."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

ROOT = Path(__file__).resolve().parents[1]
ComposeMode = Literal["rootful", "rootless"]
_ROOTLESS_TRANSPORT_URL = "http://rootless-docker-transport:2375"
_RUNNER_IMAGE = "jhin-sandbox-runner:local"
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


def _path_lstat(path: Path) -> StatResult:
    return path.lstat()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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
    command = ["docker", "compose"]
    for filename in compose_files(mode, dev=dev):
        command.extend(("-f", filename))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=_clean_environment(env),
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


def _assert_common_workers(services: Mapping[str, Any], *, dev: bool) -> None:
    agent = cast(dict[str, Any], services["agent-worker"])
    tool = cast(dict[str, Any], services["tool-worker"])
    runner = cast(dict[str, Any], services["sandbox-runner"])
    expected_app_env = "test" if dev else "production"

    _require(agent["command"] == ["jhin-agent-worker"], "agent command drifted")
    _require(tool["command"] == ["jhin-tool-worker"], "tool command drifted")
    _require(agent["environment"]["APP_ENV"] == expected_app_env, "agent APP_ENV drifted")
    _require(tool["environment"]["APP_ENV"] == expected_app_env, "tool APP_ENV drifted")
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
) -> None:
    runner = cast(dict[str, Any], services["sandbox-runner"])
    adapter = cast(dict[str, Any], services.get("rootless-docker-transport"))
    _require(adapter is not None, "rootless authority overlay is missing")
    runner_environment = cast(dict[str, Any], runner["environment"])
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
    _require(
        adapter.get("command") == ["python", "-m", "jhin_sandbox_runner.rootless_transport"],
        "adapter entrypoint drifted",
    )
    _require(
        adapter["healthcheck"]["test"]
        == [
            "CMD",
            "python",
            "-c",
            "import urllib.request;response=urllib.request.urlopen("
            "'http://127.0.0.1:2375/_ping',timeout=2);body=response.read();"
            "raise SystemExit(0 if response.status == 200 and body == b'OK' else 1)",
        ],
        "adapter health must require Docker /_ping status/body",
    )
    volumes = adapter.get("volumes", [])
    _require(len(volumes) == 1, "adapter must have one socket bind")
    volume = volumes[0]
    _require(
        volume
        == {
            "type": "bind",
            "source": volume.get("source"),
            "target": "/run/host/docker.sock",
            "bind": {},
        }
        and str(volume["source"]).startswith("/"),
        "adapter socket bind must be canonical long syntax",
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
    expected_rootful_gid: int | None = None,
    expected_socket_source: str | None = None,
) -> None:
    """Assert the one exhaustive production/dev x rootful/rootless contract."""
    _require(mode in {"rootful", "rootless"}, "mode must be rootful or rootless")
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

    _assert_common_workers(services, dev=dev)
    _assert_dev_contract(services, dev=dev)
    _assert_ports(services, mode=selected, dev=dev)
    if selected == "rootless":
        _assert_rootless(config, services)
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
    if args.dev and "APP_ENV" in os.environ:
        explicit_env["APP_ENV"] = os.environ["APP_ENV"]
    rendered = render_compose(mode, dev=bool(args.dev), env=explicit_env)
    assert_rendered_contract(
        rendered,
        mode=mode,
        dev=bool(args.dev),
        expected_rootful_gid=expected_gid,
        expected_socket_source=expected_socket,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
