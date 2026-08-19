"""Live Linux acceptance for the two sandbox Docker-socket authorities."""

from __future__ import annotations

import os
from typing import Any

import pytest

from .conftest import compose_authority
from .phase10_upgrade_harness import SocketMetadata

pytestmark = pytest.mark.integration


def _networks(container: dict[str, Any]) -> set[str]:
    networks = container.get("NetworkSettings", {}).get("Networks", {})
    assert isinstance(networks, dict)
    return set(networks)


def _socket_mounts(container: dict[str, Any], socket_path: str) -> list[dict[str, Any]]:
    mounts = container.get("Mounts", [])
    assert isinstance(mounts, list)
    return [mount for mount in mounts if mount.get("Source") == socket_path]


def test_selected_socket_mode_live_boundary() -> None:
    mode = os.environ.get("PHASE10_SOCKET_MODE")
    if mode is None:
        pytest.skip("socket-mode acceptance was not requested")
    assert mode in {"rootful", "rootless"}
    authority = compose_authority()
    assert authority.mode == mode
    authority.assert_socket_unchanged()
    authority.assert_ready()

    runner = authority.inspect_service("sandbox-runner")
    assert runner["Config"]["User"] == "10001:10001"
    assert runner["HostConfig"]["Privileged"] is False
    assert runner["HostConfig"]["CapDrop"] == ["ALL"]

    if mode == "rootful":
        assert authority.socket_gid is not None
        assert runner["HostConfig"]["GroupAdd"] == [str(authority.socket_gid)]
        mounts = _socket_mounts(runner, str(authority.socket_path))
        assert len(mounts) == 1
        assert mounts[0]["Destination"] == "/run/jhin/docker.sock"
    else:
        authority.probe_rootless_capabilities()
        assert runner["HostConfig"].get("GroupAdd", []) == []
        assert _socket_mounts(runner, str(authority.socket_path)) == []
        assert not any("docker.sock" in item for item in runner["Config"].get("Env", []))
        assert _networks(runner) == {
            f"{authority.project}_engine",
            f"{authority.project}_runner",
        }

        adapter = authority.inspect_service("rootless-docker-transport")
        assert adapter["Config"]["User"] == "0:0"
        assert adapter["HostConfig"]["Privileged"] is False
        assert adapter["HostConfig"]["CapDrop"] == ["ALL"]
        assert adapter["HostConfig"]["ReadonlyRootfs"] is True
        assert adapter["HostConfig"].get("PortBindings") in ({}, None)
        assert _networks(adapter) == {f"{authority.project}_engine"}
        adapter_mounts = _socket_mounts(adapter, str(authority.socket_path))
        assert len(adapter_mounts) == 1
        assert adapter_mounts[0]["Destination"] == "/run/host/docker.sock"
        assert authority.inspect_socket_from_adapter() == {"gid": 0, "socket": True, "uid": 0}

        for service in ("agent-worker", "tool-worker"):
            inspected = authority.inspect_service(service)
            assert f"{authority.project}_engine" not in _networks(inspected)
            assert _socket_mounts(inspected, str(authority.socket_path)) == []
        assert authority.service_dns_probe("tool-worker", "rootless-docker-transport") != 0
        assert authority.adapter_ping_from_runner() == b"OK"
        version = authority.service_http_json(
            "sandbox-runner", "http://rootless-docker-transport:2375/version"
        )
        assert isinstance(version.get("Version"), str) and version["Version"]
        assert isinstance(version.get("ApiVersion"), str) and version["ApiVersion"]

    job = authority.run_noop_sandbox_job()
    assert job["status"] == "completed", job
    assert job["exit_code"] == 0, job
    assert job["stdout"] == "phase10-noop\n", job
    authority.assert_socket_unchanged()


def test_rootful_wrong_gid_fails_closed_without_socket_mutation() -> None:
    assert os.environ.get("PHASE10_SOCKET_MODE") == "rootful"
    assert os.environ.get("JHIN_PHASE10_SCENARIO") == "wrong-gid"
    authority = compose_authority()
    before = SocketMetadata.capture(authority.socket_path)
    assert authority.socket_snapshot == before
    result = authority.run_wrong_gid_probe()
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert (
        "Docker socket group does not match SANDBOX_DOCKER_GID" in combined
        or "runner requires the exact Docker socket group only" in combined
    )
    assert SocketMetadata.capture(authority.socket_path) == before
