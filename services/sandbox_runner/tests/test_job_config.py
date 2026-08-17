"""Unit tests for the security-relevant container configuration (plan 14.3,
48.7) and the request cap validation — no Docker daemon required."""

from __future__ import annotations

import pytest

from jhin_sandbox_runner.jobs import (
    JobValidationError,
    build_container_config,
    resolve_limits,
    workspace_volume_name,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings

SETTINGS = Settings(
    sandbox_runner_token="test-token",
    sandbox_default_image="jhin-sandbox:test",
    sandbox_network="jhin_sandbox_test",
)


def request(**overrides: object) -> SandboxJobRequest:
    base: dict[str, object] = {
        "job_id": "0123456789abcdef",
        "command": ["bash", "-lc", "echo hi"],
    }
    base.update(overrides)
    return SandboxJobRequest.model_validate(base)


def config_for(req: SandboxJobRequest) -> dict[str, object]:
    cpu, memory, pids, _ = resolve_limits(req, SETTINGS)
    return build_container_config(
        req,
        SETTINGS,
        image=req.image or SETTINGS.sandbox_default_image,
        cpu_limit=cpu,
        memory_mb=memory,
        pids_limit=pids,
    )


class TestIsolationInvariants:
    def test_never_privileged_and_caps_dropped(self) -> None:
        host = config_for(request())["HostConfig"]
        assert host["Privileged"] is False
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["ReadonlyRootfs"] is True

    def test_no_docker_socket_or_host_mounts(self) -> None:
        """Plan 48.7: jobs never receive the Docker socket or host paths.
        The only bind, ever, is the named workspace volume."""
        host = config_for(request(workspace_key="run-abc"))["HostConfig"]
        binds = host.get("Binds", [])
        assert binds == ["jhin-sandbox-ws-run-abc:/workspace"]
        assert not any("docker.sock" in b or b.startswith("/") for b in binds)
        host_no_ws = config_for(request())["HostConfig"]
        assert "Binds" not in host_no_ws

    def test_network_policy_mapping(self) -> None:
        assert config_for(request())["HostConfig"]["NetworkMode"] == "none"
        internet = config_for(request(network_policy="internet"))
        # A dedicated sandbox bridge — never "host", never a control network.
        assert internet["HostConfig"]["NetworkMode"] == "jhin_sandbox_test"

    def test_runs_as_non_root_uid_1000(self) -> None:
        assert config_for(request())["User"] == "1000:1000"

    def test_resource_limits_applied(self) -> None:
        req = request(cpu_limit=1.5, memory_mb=512, pids_limit=64)
        cpu, memory, pids, _ = resolve_limits(req, SETTINGS)
        config = build_container_config(
            req, SETTINGS, image="x", cpu_limit=cpu, memory_mb=memory, pids_limit=pids
        )
        host = config["HostConfig"]
        assert host["NanoCpus"] == 1_500_000_000
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["MemorySwap"] == host["Memory"]  # no extra swap headroom
        assert host["PidsLimit"] == 64

    def test_tmp_is_tmpfs_and_home_is_workspace(self) -> None:
        config = config_for(request())
        assert "/tmp" in config["HostConfig"]["Tmpfs"]
        assert "HOME=/workspace" in config["Env"]

    def test_secret_env_injected(self) -> None:
        config = config_for(request(secret_env={"GIT_TOKEN": "hunter22"}))
        assert "GIT_TOKEN=hunter22" in config["Env"]

    def test_job_label_present_for_reaping(self) -> None:
        config = config_for(request())
        assert config["Labels"] == {"jhin.sandbox.job": "0123456789abcdef"}


class TestCaps:
    def test_defaults_are_the_caps(self) -> None:
        assert resolve_limits(request(), SETTINGS) == (2.0, 4096, 256, 1800)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cpu_limit": 3.0},
            {"memory_mb": 8192},
            {"pids_limit": 1000},
            {"timeout_seconds": 7200},
        ],
    )
    def test_requests_above_cap_rejected(self, overrides: dict[str, object]) -> None:
        with pytest.raises(JobValidationError):
            resolve_limits(request(**overrides), SETTINGS)

    def test_requests_below_cap_pass_through(self) -> None:
        req = request(cpu_limit=0.5, memory_mb=256, pids_limit=32, timeout_seconds=30)
        assert resolve_limits(req, SETTINGS) == (0.5, 256, 32, 30)


class TestRequestValidation:
    def test_rejects_bad_env_names(self) -> None:
        with pytest.raises(ValueError):
            request(env={"lower;rm -rf": "x"})

    def test_rejects_bad_workspace_key(self) -> None:
        with pytest.raises(ValueError):
            request(workspace_key="../../etc")

    def test_rejects_relative_working_dir(self) -> None:
        with pytest.raises(ValueError):
            request(working_dir="workspace")

    def test_volume_name_shape(self) -> None:
        assert workspace_volume_name("run-1") == "jhin-sandbox-ws-run-1"
