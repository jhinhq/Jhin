"""Unit tests for the security-relevant container configuration (plan 14.3,
48.7) and the request cap validation — no Docker daemon required."""

from __future__ import annotations

from pathlib import Path

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
    sandbox_docker_mode="rootless",
    sandbox_docker_transport_url="http://rootless-docker-transport:2375",
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
        The only mount, ever, is the named workspace volume."""
        host = config_for(request(workspace_key="run-abc"))["HostConfig"]
        assert "Binds" not in host
        mounts = host.get("Mounts", [])
        assert mounts == [
            {
                "Type": "volume",
                "Source": "jhin-sandbox-ws-run-abc",
                "Target": "/workspace",
                "ReadOnly": False,
                "VolumeOptions": {"NoCopy": True},
            }
        ]
        assert "docker.sock" not in repr(mounts)
        host_no_ws = config_for(request())["HostConfig"]
        assert "Binds" not in host_no_ws and "Mounts" not in host_no_ws

    def test_network_policy_mapping(self) -> None:
        assert config_for(request())["HostConfig"]["NetworkMode"] == "none"
        internet = config_for(request(network_policy="internet"))
        # A dedicated sandbox bridge — never "host", never a control network.
        assert internet["HostConfig"]["NetworkMode"] == "jhin_sandbox_test"

    @pytest.mark.parametrize("network", ["runner", "engine"])
    def test_control_plane_network_is_rejected(self, network: str) -> None:
        unsafe_settings = SETTINGS.model_copy(update={"sandbox_network": network})
        req = request(network_policy="internet")
        cpu, memory, pids, _timeout = resolve_limits(req, unsafe_settings)
        with pytest.raises(JobValidationError, match="control-plane network"):
            build_container_config(
                req,
                unsafe_settings,
                image="jhin-sandbox:test",
                cpu_limit=cpu,
                memory_mb=memory,
                pids_limit=pids,
            )

    def test_runs_as_non_root_uid_1000(self) -> None:
        config = config_for(request())
        assert config["User"] == "1000:1000"
        assert "GroupAdd" not in config["HostConfig"]

    def test_job_never_inherits_runner_docker_authority(self) -> None:
        config = config_for(
            request(
                env={
                    "SANDBOX_DOCKER_MODE": "rootful",
                    "DOCKER_HOST": "unix:///run/host/docker.sock",
                }
            )
        )
        host = config["HostConfig"]
        assert all(
            forbidden not in repr(config)
            for forbidden in (
                "/var/run/docker.sock",
                "/run/jhin/docker.sock",
                "/run/host/docker.sock",
                "rootless-docker-transport",
                "SANDBOX_DOCKER_",
                "DOCKER_HOST",
            )
        )
        assert host["NetworkMode"] not in {"runner", "engine"}

    @pytest.mark.parametrize("field", ["env", "secret_env"])
    @pytest.mark.parametrize(
        "forbidden_name",
        [
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_API_VERSION",
            "DOCKER_CUSTOM_AUTHORITY",
            "SANDBOX_DOCKER_MODE",
            "SANDBOX_DOCKER_SOCKET",
            "SANDBOX_DOCKER_TRANSPORT_URL",
            "SANDBOX_DOCKER_GID",
            "SANDBOX_DOCKER_CUSTOM_AUTHORITY",
        ],
    )
    def test_all_docker_authority_variable_names_are_removed(
        self, field: str, forbidden_name: str
    ) -> None:
        config = config_for(
            request(**{field: {forbidden_name: "not-authority", "KEEP_ME": "normal-secret"}})
        )
        environment = dict(entry.split("=", 1) for entry in config["Env"])
        assert forbidden_name not in environment
        assert environment["KEEP_ME"] == "normal-secret"

    @pytest.mark.parametrize("field", ["env", "secret_env"])
    @pytest.mark.parametrize(
        "forbidden_value",
        [
            "rootless-docker-transport",
            "rootless-docker-transport:2375",
            "http://rootless-docker-transport:2375",
            "tcp://rootless-docker-transport:2375",
            "https://rootless-docker-transport:2375/v1.47",
            "/var/run/docker.sock",
            "unix:///var/run/docker.sock",
            "/run/jhin/docker.sock",
            "file:///run/jhin/docker.sock",
            "/run/host/docker.sock",
            "unix:///run/host/docker.sock",
        ],
    )
    def test_all_docker_authority_value_forms_are_removed(
        self, field: str, forbidden_value: str
    ) -> None:
        config = config_for(
            request(**{field: {"AUTHORITY_ALIAS": forbidden_value, "KEEP_ME": "safe"}})
        )
        environment = dict(entry.split("=", 1) for entry in config["Env"])
        assert "AUTHORITY_ALIAS" not in environment
        assert environment["KEEP_ME"] == "safe"

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


def test_python_runtime_image_creates_exact_numeric_runner_identity() -> None:
    dockerfile = (Path(__file__).parents[3] / "docker" / "python.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "groupadd --gid 10001 jhin" in dockerfile
    assert "useradd --create-home --uid 10001 --gid 10001 jhin" in dockerfile
