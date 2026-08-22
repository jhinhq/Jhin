"""Job lifecycle publication tests for the sandbox runner."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast

import pytest

import jhin_sandbox_runner.jobs as jobs_module
from jhin_sandbox_runner.docker_socket import DockerSocketConfigurationError
from jhin_sandbox_runner.jobs import (
    DockerDaemonConfigurationError,
    JobManager,
    JobRecord,
)
from jhin_sandbox_runner.schemas import SandboxJobRequest
from jhin_sandbox_runner.settings import Settings


class _BlockingDeleteContainer:
    id = "sandbox-container-id"

    def __init__(self) -> None:
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()
        self.killed = False

    async def start(self) -> None:
        return None

    async def show(self) -> dict[str, dict[str, int | bool]]:
        return {"State": {"Running": not self.killed, "ExitCode": 137}}

    async def kill(self) -> None:
        self.killed = True

    async def log(self, **_kwargs: bool) -> list[str]:
        return []

    async def delete(self, *, force: bool, v: bool) -> None:
        self.delete_started.set()
        await self.allow_delete.wait()


class _FakeContainers:
    def __init__(self, container: _BlockingDeleteContainer) -> None:
        self._container = container

    async def create(self, _config: dict[str, Any], *, name: str) -> _BlockingDeleteContainer:
        return self._container


class _FakeDocker:
    def __init__(self, container: _BlockingDeleteContainer) -> None:
        self.containers = _FakeContainers(container)


class _WorkspaceInitializerContainer:
    id = "workspace-initializer-id"

    def __init__(self, *, status_code: int = 0) -> None:
        self.status_code = status_code
        self.started = False
        self.wait_timeout: float | None = None
        self.deleted: tuple[bool, bool] | None = None

    async def start(self) -> None:
        self.started = True

    async def wait(self, **kwargs: float) -> dict[str, int]:
        self.wait_timeout = kwargs["timeout"]
        return {"StatusCode": self.status_code}

    async def delete(self, *, force: bool, v: bool) -> None:
        self.deleted = (force, v)


class _WorkspaceVolumes:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def create(self, config: dict[str, object]) -> None:
        self.created.append(config)


class _WorkspaceContainers:
    def __init__(self, container: _WorkspaceInitializerContainer) -> None:
        self.container = container
        self.created: list[tuple[dict[str, Any], str]] = []

    async def create(self, config: dict[str, Any], *, name: str) -> _WorkspaceInitializerContainer:
        self.created.append((config, name))
        return self.container


class _WorkspaceDocker:
    def __init__(self, container: _WorkspaceInitializerContainer) -> None:
        self.volumes = _WorkspaceVolumes()
        self.containers = _WorkspaceContainers(container)


@pytest.mark.asyncio
async def test_workspace_volume_is_nocopy_and_initialized_by_trusted_root() -> None:
    container = _WorkspaceInitializerContainer()
    docker = _WorkspaceDocker(container)
    manager = JobManager(runner_settings(sandbox_default_image="jhin-sandbox:test"))
    manager._docker = cast(Any, docker)

    await manager._ensure_workspace_volume("run-abc", job_id="0123456789abcdef")

    assert docker.volumes.created == [
        {
            "Name": "jhin-sandbox-ws-run-abc",
            "Labels": {"jhin.sandbox.workspace": "run-abc"},
        }
    ]
    assert len(docker.containers.created) == 1
    config, name = docker.containers.created[0]
    assert name == "jhin-sbx-init-0123456789abcdef"
    assert config["Image"] == "jhin-sandbox:test"
    assert config["Entrypoint"] == ["python3", "-c"]
    assert isinstance(config["Cmd"], list) and len(config["Cmd"]) == 1
    assert "os.chmod" in config["Cmd"][0] and "os.chown" in config["Cmd"][0]
    assert config["User"] == "0:0"
    assert config["Labels"] == {"jhin.sandbox.workspace.init": "run-abc"}
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert host["CapAdd"] == ["CHOWN", "FOWNER"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Mounts"] == [
        {
            "Type": "volume",
            "Source": "jhin-sandbox-ws-run-abc",
            "Target": "/jhin-workspace-init",
            "ReadOnly": False,
            "VolumeOptions": {"NoCopy": True},
        }
    ]
    assert container.started is True
    assert container.wait_timeout == jobs_module.DOCKER_CHECK_TIMEOUT_SECONDS
    assert container.deleted == (True, True)


@pytest.mark.asyncio
async def test_workspace_initializer_failure_is_terminal_and_deleted() -> None:
    container = _WorkspaceInitializerContainer(status_code=1)
    docker = _WorkspaceDocker(container)
    manager = JobManager(runner_settings(sandbox_default_image="jhin-sandbox:test"))
    manager._docker = cast(Any, docker)

    with pytest.raises(RuntimeError, match="workspace initialization failed"):
        await manager._ensure_workspace_volume("run-abc", job_id="0123456789abcdef")
    assert container.deleted == (True, True)


def runner_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sandbox_runner_token": "test-token",
        "sandbox_docker_mode": "rootless",
        "sandbox_docker_transport_url": "http://rootless-docker-transport:2375",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_terminal_status_waits_for_container_deletion() -> None:
    """A terminal API state guarantees its ephemeral container is gone."""
    request = SandboxJobRequest(
        job_id="0123456789abcdef",
        command=["bash", "-c", "sleep 120"],
        timeout_seconds=1,
    )
    record = JobRecord(
        request=request,
        image="jhin-sandbox:test",
        cpu_limit=1.0,
        memory_mb=128,
        pids_limit=16,
        timeout_seconds=0,
    )
    container = _BlockingDeleteContainer()
    manager = JobManager(runner_settings())
    manager._docker = cast(Any, _FakeDocker(container))

    run_task = asyncio.create_task(manager._run(record))
    try:
        await asyncio.wait_for(container.delete_started.wait(), timeout=1.0)
        assert record.status == "running"
        assert record.finished_at is None
    finally:
        container.allow_delete.set()
        await run_task

    assert record.status == "timeout"
    assert record.finished_at is not None


class _StartupSystem:
    def __init__(self, owner: _StartupDocker) -> None:
        self.owner = owner

    async def info(self) -> dict[str, object]:
        self.owner.calls.append("system.info")
        self.owner.fail_if("system.info")
        info: dict[str, object] = {"SecurityOptions": self.owner.security_options}
        operating_system = type(self.owner).operating_system
        if operating_system is not _UNSET:
            info["OperatingSystem"] = operating_system
        return info


class _StartupNetworks:
    def __init__(self, owner: _StartupDocker) -> None:
        self.owner = owner

    async def list(self, **_kwargs: object) -> list[dict[str, str]]:
        self.owner.calls.append("networks.list")
        self.owner.fail_if("networks.list")
        return [{"Name": "jhin_sandbox"}]

    async def create(self, _config: dict[str, object]) -> None:
        self.owner.calls.append("networks.create")


class _StartupContainers:
    def __init__(self, owner: _StartupDocker) -> None:
        self.owner = owner

    async def list(self, **kwargs: object) -> list[object]:
        self.owner.calls.append("containers.list")
        self.owner.container_list_kwargs.append(kwargs)
        self.owner.fail_if("containers.list")
        return [self.owner.orphan] if self.owner.orphan is not None else []


class _FailingOrphan:
    id = "orphan-container-id"

    def __init__(self, owner: _StartupDocker) -> None:
        self.owner = owner

    async def delete(self, *, force: bool, v: bool) -> None:
        self.owner.calls.append("container.delete")
        raise RuntimeError("failed orphan delete")


class _StartupVolumes:
    def __init__(self, owner: _StartupDocker) -> None:
        self.owner = owner

    async def list(self, **_kwargs: object) -> dict[str, list[object]]:
        self.owner.calls.append("volumes.list")
        self.owner.fail_if("volumes.list")
        return {"Volumes": []}


_UNSET: object = object()


class _StartupDocker:
    instances: ClassVar[list[_StartupDocker]] = []
    fail_stage: ClassVar[str | None] = None
    orphan: ClassVar[_FailingOrphan | None] = None
    security_options: ClassVar[list[str]] = ["name=rootless"]
    operating_system: ClassVar[object] = _UNSET

    def __init__(self, *, url: str) -> None:
        self.url = url
        self.calls: list[str] = []
        self.container_list_kwargs: list[dict[str, object]] = []
        self.closed = False
        self.system = _StartupSystem(self)
        self.networks = _StartupNetworks(self)
        self.containers = _StartupContainers(self)
        self.volumes = _StartupVolumes(self)
        type(self).instances.append(self)

    def fail_if(self, stage: str) -> None:
        if type(self).fail_stage == stage:
            raise RuntimeError(f"failed {stage}")

    async def version(self) -> dict[str, str]:
        self.calls.append("version")
        if type(self).fail_stage == "version-timeout":
            await asyncio.Event().wait()
        self.fail_if("version")
        return {"ApiVersion": "1.47"}

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True


@pytest.fixture
def startup_docker(monkeypatch: pytest.MonkeyPatch) -> type[_StartupDocker]:
    _StartupDocker.instances = []
    _StartupDocker.fail_stage = None
    _StartupDocker.orphan = None
    _StartupDocker.security_options = ["name=rootless"]
    _StartupDocker.operating_system = _UNSET
    monkeypatch.setattr(jobs_module.aiodocker, "Docker", _StartupDocker)
    monkeypatch.setattr(jobs_module.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(jobs_module.os, "getegid", lambda: 10001)
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: [10001])
    return _StartupDocker


@pytest.mark.asyncio
async def test_start_validates_daemon_identity_before_any_mutation(
    startup_docker: type[_StartupDocker],
) -> None:
    manager = JobManager(runner_settings())
    await manager.start()
    docker = startup_docker.instances[0]
    assert docker.url == "http://rootless-docker-transport:2375"
    assert docker.calls == [
        "version",
        "system.info",
        "networks.list",
        "containers.list",
        "containers.list",
        "volumes.list",
    ]
    assert docker.container_list_kwargs == [
        {"all": 1, "filters": '{"label": ["jhin.sandbox.job"]}'},
        {"all": 1, "filters": '{"label": ["jhin.sandbox.workspace.init"]}'},
    ]


@pytest.mark.parametrize("options", [[], ["name=seccomp,profile=default"], ["Name=Rootless"]])
@pytest.mark.asyncio
async def test_rootless_start_rejects_daemon_without_exact_identity(
    startup_docker: type[_StartupDocker], options: list[str]
) -> None:
    startup_docker.security_options = options
    manager = JobManager(runner_settings())
    with pytest.raises(DockerDaemonConfigurationError, match="not rootless"):
        await manager.start()
    docker = startup_docker.instances[0]
    assert docker.calls == ["version", "system.info", "close"]
    assert docker.closed is True
    assert manager._docker is None


@pytest.mark.parametrize(
    ("stage", "expected_calls"),
    [
        ("version", ["version", "close"]),
        ("system.info", ["version", "system.info", "close"]),
        (
            "networks.list",
            ["version", "system.info", "networks.list", "close"],
        ),
        (
            "containers.list",
            [
                "version",
                "system.info",
                "networks.list",
                "containers.list",
                "close",
            ],
        ),
        (
            "volumes.list",
            [
                "version",
                "system.info",
                "networks.list",
                "containers.list",
                "containers.list",
                "volumes.list",
                "close",
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_failed_startup_stage_closes_and_resets_client(
    startup_docker: type[_StartupDocker], stage: str, expected_calls: list[str]
) -> None:
    startup_docker.fail_stage = stage
    manager = JobManager(runner_settings())
    with pytest.raises(RuntimeError, match=f"failed {stage}"):
        await manager.start()
    docker = startup_docker.instances[0]
    assert docker.calls == expected_calls
    assert docker.closed is True
    assert manager._docker is None


@pytest.mark.asyncio
async def test_daemon_identity_checks_are_bounded(
    startup_docker: type[_StartupDocker], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup_docker.fail_stage = "version-timeout"
    monkeypatch.setattr(jobs_module, "DOCKER_CHECK_TIMEOUT_SECONDS", 0.01)
    manager = JobManager(runner_settings())
    with pytest.raises(TimeoutError):
        await manager.start()
    docker = startup_docker.instances[0]
    assert docker.calls == ["version", "close"]
    assert manager._docker is None


@pytest.mark.asyncio
async def test_matching_orphan_delete_failure_prevents_readiness(
    startup_docker: type[_StartupDocker], monkeypatch: pytest.MonkeyPatch
) -> None:
    docker_holder: list[_StartupDocker] = []

    class DockerWithOrphan(_StartupDocker):
        def __init__(self, *, url: str) -> None:
            super().__init__(url=url)
            type(self).orphan = _FailingOrphan(self)
            docker_holder.append(self)

    startup_docker.instances = []
    monkeypatch.setattr(jobs_module.aiodocker, "Docker", DockerWithOrphan)
    manager = JobManager(runner_settings())
    with pytest.raises(RuntimeError, match="failed orphan delete"):
        await manager.start()
    docker = docker_holder[0]
    assert docker.calls == [
        "version",
        "system.info",
        "networks.list",
        "containers.list",
        "container.delete",
        "close",
    ]
    assert manager._docker is None


@pytest.mark.parametrize(("uid", "gid"), [(10002, 10001), (10001, 10002)])
@pytest.mark.asyncio
async def test_rootless_start_requires_exact_runtime_identity_before_connect(
    startup_docker: type[_StartupDocker],
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
) -> None:
    monkeypatch.setattr(jobs_module.os, "geteuid", lambda: uid)
    monkeypatch.setattr(jobs_module.os, "getegid", lambda: gid)
    manager = JobManager(runner_settings())
    with pytest.raises(DockerSocketConfigurationError, match="10001"):
        await manager.start()
    assert startup_docker.instances == []


@pytest.mark.parametrize("process_groups", [[10001], [10001, 10001], []])
@pytest.mark.asyncio
async def test_rootless_start_accepts_empty_or_primary_only_process_groups(
    startup_docker: type[_StartupDocker],
    monkeypatch: pytest.MonkeyPatch,
    process_groups: list[int],
) -> None:
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: process_groups)

    manager = JobManager(runner_settings())
    await manager.start()

    assert len(startup_docker.instances) == 1


@pytest.mark.parametrize(
    "process_groups",
    [[20002], [10001, 20002], [0, 10001], [0, 10001, 20002]],
)
@pytest.mark.asyncio
async def test_rootless_start_rejects_every_non_primary_process_group(
    startup_docker: type[_StartupDocker],
    monkeypatch: pytest.MonkeyPatch,
    process_groups: list[int],
) -> None:
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: process_groups)

    manager = JobManager(runner_settings())
    with pytest.raises(DockerSocketConfigurationError, match="no supplemental groups"):
        await manager.start()

    assert startup_docker.instances == []


@pytest.mark.parametrize(
    ("process_groups", "accepted"),
    [
        ([10001, 12001], True),
        ([10001, 12001, 12001], True),
        ([12001], True),
        ([12001, 12001], True),
        ([], False),
        ([10001], False),
        ([10001, 13000], False),
        ([10001, 12001, 13000], False),
        ([0, 12001], False),
    ],
)
@pytest.mark.asyncio
async def test_rootful_start_allows_only_the_normalized_socket_group(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    process_groups: list[int],
    accepted: bool,
) -> None:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    fake_stat = type("SocketStat", (), {"st_mode": 0o140000, "st_uid": 0, "st_gid": 12001})()
    monkeypatch.setattr(type(socket_path), "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(
        jobs_module.os,
        "access",
        lambda _path, _mode, *, effective_ids: True,
    )
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: process_groups)
    startup_docker.security_options = []
    manager = JobManager(
        runner_settings(
            sandbox_docker_mode="rootful",
            sandbox_docker_socket=socket_path,
            sandbox_docker_transport_url=None,
            sandbox_docker_gid=12001,
        )
    )

    if accepted:
        await manager.start()
        assert len(startup_docker.instances) == 1
    else:
        with pytest.raises(DockerSocketConfigurationError, match="exact Docker socket group"):
            await manager.start()
        assert startup_docker.instances == []


@pytest.mark.parametrize(
    ("process_groups", "accepted"),
    [
        ([], True),
        ([10001], True),
        ([10001, 10001], True),
        ([20002], False),
        ([10001, 20002], False),
        ([10001, 10001, 20002], False),
        ([0], False),
        ([0, 10001], False),
    ],
)
@pytest.mark.asyncio
async def test_rootful_start_accepts_primary_gid_as_the_exact_socket_authority(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    process_groups: list[int],
    accepted: bool,
) -> None:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    fake_stat = type("SocketStat", (), {"st_mode": 0o140000, "st_uid": 0, "st_gid": 10001})()
    monkeypatch.setattr(type(socket_path), "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(
        jobs_module.os,
        "access",
        lambda _path, _mode, *, effective_ids: True,
    )
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: process_groups)
    startup_docker.security_options = []
    manager = JobManager(
        runner_settings(
            sandbox_docker_mode="rootful",
            sandbox_docker_socket=socket_path,
            sandbox_docker_transport_url=None,
            sandbox_docker_gid=10001,
        )
    )

    if accepted:
        await manager.start()
        assert len(startup_docker.instances) == 1
    else:
        with pytest.raises(DockerSocketConfigurationError, match="exact Docker socket group"):
            await manager.start()
        assert startup_docker.instances == []


@pytest.mark.asyncio
async def test_rootful_start_does_not_require_rootless_security_option(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    fake_stat = type("SocketStat", (), {"st_mode": 0o140000, "st_uid": 0, "st_gid": 12001})()
    monkeypatch.setattr(type(socket_path), "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(
        jobs_module.os,
        "access",
        lambda _path, _mode, *, effective_ids: True,
    )
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: [12001])
    startup_docker.security_options = []
    manager = JobManager(
        runner_settings(
            sandbox_docker_mode="rootful",
            sandbox_docker_socket=socket_path,
            sandbox_docker_transport_url=None,
            sandbox_docker_gid=12001,
        )
    )
    await manager.start()
    assert startup_docker.instances[0].calls[:2] == ["version", "system.info"]


def _desktop_manager(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_groups: list[int],
) -> JobManager:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    fake_stat = type("SocketStat", (), {"st_mode": 0o140000, "st_uid": 0, "st_gid": 0})()
    monkeypatch.setattr(type(socket_path), "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(jobs_module.os, "access", lambda _path, _mode, *, effective_ids: True)
    monkeypatch.setattr(jobs_module.os, "getgroups", lambda: process_groups)
    return JobManager(
        runner_settings(
            sandbox_docker_mode="desktop",
            sandbox_docker_socket=socket_path,
            sandbox_docker_transport_url=None,
            sandbox_docker_gid=None,
        )
    )


@pytest.mark.asyncio
async def test_desktop_start_accepts_a_docker_desktop_daemon_through_the_root_group(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_docker.security_options = ["name=seccomp,profile=builtin", "name=cgroupns"]
    startup_docker.operating_system = "Docker Desktop"
    manager = _desktop_manager(tmp_path, monkeypatch, process_groups=[10001, 0])
    await manager.start()
    docker = startup_docker.instances[0]
    assert docker.url == f"unix://{tmp_path / 'docker.sock'}"
    assert docker.calls[:2] == ["version", "system.info"]
    assert manager._docker is docker


@pytest.mark.parametrize(
    "process_groups",
    [[], [10001], [12001], [10001, 12001], [0, 12001]],
)
@pytest.mark.asyncio
async def test_desktop_start_requires_exactly_the_root_group(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    process_groups: list[int],
) -> None:
    startup_docker.operating_system = "Docker Desktop"
    manager = _desktop_manager(tmp_path, monkeypatch, process_groups=process_groups)
    with pytest.raises(DockerSocketConfigurationError, match="root group as its only"):
        await manager.start()
    assert startup_docker.instances == []


@pytest.mark.parametrize("operating_system", ["Ubuntu 24.04 LTS", "", None, 3])
@pytest.mark.asyncio
async def test_desktop_start_rejects_a_non_desktop_daemon_before_any_mutation(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    operating_system: object,
) -> None:
    startup_docker.security_options = []
    startup_docker.operating_system = operating_system
    manager = _desktop_manager(tmp_path, monkeypatch, process_groups=[0])
    with pytest.raises(DockerDaemonConfigurationError, match="Docker Desktop"):
        await manager.start()
    docker = startup_docker.instances[0]
    assert docker.calls == ["version", "system.info", "close"]
    assert docker.closed is True
    assert manager._docker is None


@pytest.mark.asyncio
async def test_desktop_start_requires_runner_gid_10001(
    startup_docker: type[_StartupDocker],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_docker.operating_system = "Docker Desktop"
    manager = _desktop_manager(tmp_path, monkeypatch, process_groups=[0])
    monkeypatch.setattr(jobs_module.os, "getegid", lambda: 0)
    with pytest.raises(DockerSocketConfigurationError, match="10001:10001"):
        await manager.start()
    assert startup_docker.instances == []
