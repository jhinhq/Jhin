"""Fail-closed tests for the sandbox runner's Docker authority boundary."""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

import jhin_sandbox_runner.docker_socket as docker_socket_module
from jhin_sandbox_runner.docker_socket import (
    ROOTLESS_TRANSPORT_URL,
    DockerSocketConfigurationError,
    validate_docker_authority,
)
from jhin_sandbox_runner.settings import Settings


@pytest.fixture
def unix_socket() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="jhin-socket-", dir="/tmp"))
    path = directory / "docker.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    try:
        yield path
    finally:
        server.close()
        path.unlink(missing_ok=True)
        directory.rmdir()


def _rootful_kwargs(socket_path: Path) -> dict[str, object]:
    return {
        "mode": "rootful",
        "socket_path": socket_path,
        "transport_url": None,
        "configured_gid": 12001,
        "effective_uid": 10001,
        "supplemental_groups": {12001},
    }


def _root_owned_socket_stat(*, gid: int = 12001, mode: int = stat.S_IFSOCK) -> object:
    return SimpleNamespace(st_mode=mode, st_uid=0, st_gid=gid)


@pytest.mark.parametrize(
    ("effective_gid", "process_groups", "expected"),
    [
        (0, [], set()),
        (0, [0], set()),
        (10001, [10001], set()),
        (10001, [10001, 10001], set()),
        (10001, [12001], {12001}),
        (10001, [10001, 12001, 12001], {12001}),
        (10001, [0, 10001, 12001], {0, 12001}),
    ],
)
def test_group_normalization_removes_only_the_effective_primary_gid(
    effective_gid: int, process_groups: list[int], expected: set[int]
) -> None:
    assert (
        docker_socket_module.normalize_supplemental_groups(
            effective_gid=effective_gid,
            process_groups=process_groups,
        )
        == expected
    )


@pytest.mark.integration
def test_built_runtime_image_enforces_real_container_group_shapes() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")

    repository = Path(__file__).parents[3]
    image = f"jhin-sandbox-group-boundary-test:{uuid4().hex}"
    probe = "\n".join(
        [
            "import json, os",
            "from jhin_sandbox_runner.docker_socket import (",
            "    ROOTLESS_TRANSPORT_URL,",
            "    DockerSocketConfigurationError,",
            "    normalize_supplemental_groups,",
            "    validate_docker_authority,",
            ")",
            "raw_groups = os.getgroups()",
            "authority_groups = normalize_supplemental_groups(",
            "    effective_gid=os.getegid(), process_groups=raw_groups",
            ")",
            "rootless_accepted = True",
            "try:",
            "    validate_docker_authority(",
            "        mode='rootless', socket_path=None,",
            "        transport_url=ROOTLESS_TRANSPORT_URL, configured_gid=None,",
            "        effective_uid=os.geteuid(), supplemental_groups=authority_groups,",
            "    )",
            "except DockerSocketConfigurationError:",
            "    rootless_accepted = False",
            "print(json.dumps({",
            "    'euid': os.geteuid(), 'egid': os.getegid(),",
            "    'process_groups': sorted(raw_groups),",
            "    'authority_groups': sorted(authority_groups),",
            "    'rootless_accepted': rootless_accepted,",
            "}))",
        ]
    )

    def run_probe(*docker_arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            ["docker", "run", "--rm", *docker_arguments, image, "python", "-c", probe],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cast(dict[str, object], json.loads(completed.stdout.strip().splitlines()[-1]))

    try:
        subprocess.run(
            [
                "docker",
                "build",
                "--file",
                "docker/python.Dockerfile",
                "--build-arg",
                "SERVICE_PACKAGE=jhin-sandbox-runner",
                "--tag",
                image,
                ".",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )

        assert run_probe() == {
            "euid": 10001,
            "egid": 10001,
            "process_groups": [10001],
            "authority_groups": [],
            "rootless_accepted": True,
        }
        assert run_probe("--user", "0:0") == {
            "euid": 0,
            "egid": 0,
            "process_groups": [0],
            "authority_groups": [],
            "rootless_accepted": False,
        }
        assert run_probe("--user", "10001:10001", "--group-add", "20002") == {
            "euid": 10001,
            "egid": 10001,
            "process_groups": [10001, 20002],
            "authority_groups": [20002],
            "rootless_accepted": False,
        }
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_rootless_runner_requires_private_transport_and_no_socket(tmp_path: Path) -> None:
    assert (
        validate_docker_authority(
            mode="rootless",
            socket_path=None,
            transport_url=ROOTLESS_TRANSPORT_URL,
            configured_gid=None,
            effective_uid=10001,
            supplemental_groups=set(),
        )
        == "http://rootless-docker-transport:2375"
    )
    with pytest.raises(DockerSocketConfigurationError, match="no socket mount"):
        validate_docker_authority(
            mode="rootless",
            socket_path=tmp_path / "docker.sock",
            transport_url=ROOTLESS_TRANSPORT_URL,
            configured_gid=None,
            effective_uid=10001,
            supplemental_groups=set(),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"effective_uid": 0}, "must not run as root"),
        ({"effective_uid": 10002}, "UID 10001"),
        ({"transport_url": None}, "private endpoint"),
        ({"transport_url": ""}, "private endpoint"),
        ({"transport_url": "http://attacker:2375"}, "private endpoint"),
        ({"configured_gid": 10001}, "no socket GID"),
        ({"supplemental_groups": {10001}}, "no supplemental groups"),
    ],
)
def test_rootless_rejects_wrong_authority(overrides: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "mode": "rootless",
        "socket_path": None,
        "transport_url": ROOTLESS_TRANSPORT_URL,
        "configured_gid": None,
        "effective_uid": 10001,
        "supplemental_groups": set(),
    }
    values.update(overrides)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**values)  # type: ignore[arg-type]


def test_rootful_requires_exact_socket_gid_and_membership(
    unix_socket: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: _root_owned_socket_stat())
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    assert validate_docker_authority(**_rootful_kwargs(unix_socket)) == f"unix://{unix_socket}"

    for configured_gid, groups in [(12002, {12001}), (12001, set()), (12001, {12001, 12002})]:
        values = _rootful_kwargs(unix_socket)
        values.update(configured_gid=configured_gid, supplemental_groups=groups)
        with pytest.raises(DockerSocketConfigurationError):
            validate_docker_authority(**values)  # type: ignore[arg-type]


def test_rootful_socket_access_check_uses_effective_identity(
    unix_socket: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int, bool]] = []

    def record_access(path: Path, mode: int, *, effective_ids: bool = False) -> bool:
        calls.append((path, mode, effective_ids))
        return True

    monkeypatch.setattr(Path, "lstat", lambda _path: _root_owned_socket_stat())
    monkeypatch.setattr(os, "access", record_access)

    assert validate_docker_authority(**_rootful_kwargs(unix_socket)) == f"unix://{unix_socket}"
    assert calls == [(unix_socket, os.R_OK | os.W_OK, True)]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda values, _path: values.update(socket_path=None), "one Unix socket only"),
        (
            lambda values, _path: values.update(transport_url=ROOTLESS_TRANSPORT_URL),
            "one Unix socket only",
        ),
        (lambda values, _path: values.update(configured_gid=None), "positive SANDBOX_DOCKER_GID"),
        (lambda values, _path: values.update(configured_gid=0), "positive SANDBOX_DOCKER_GID"),
        (lambda values, _path: values.update(effective_uid=0), "must not run as root"),
        (lambda values, _path: values.update(socket_path=Path("docker.sock")), "absolute"),
    ],
)
def test_rootful_rejects_invalid_shape(
    unix_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Callable[[dict[str, object], Path], None],
    message: str,
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: _root_owned_socket_stat())
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    values = _rootful_kwargs(unix_socket)
    mutator(values, unix_socket)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("socket_stat", "access", "message"),
    [
        (_root_owned_socket_stat(mode=stat.S_IFREG), True, "not a Unix socket"),
        (
            SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=10001, st_gid=12001),
            True,
            "owned by UID 0",
        ),
        (_root_owned_socket_stat(gid=12002), True, "does not match SANDBOX_DOCKER_GID"),
        (_root_owned_socket_stat(), False, "not readable and writable"),
    ],
)
def test_rootful_rejects_socket_metadata_mismatch(
    unix_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
    socket_stat: object,
    access: bool,
    message: str,
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: socket_stat)
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: access)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**_rootful_kwargs(unix_socket))


def test_rootful_rejects_symlink_before_following_it(
    unix_socket: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "docker-link.sock"
    link.symlink_to(unix_socket)
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    values = _rootful_kwargs(link)
    with pytest.raises(DockerSocketConfigurationError, match="symlink"):
        validate_docker_authority(**values)


def test_rootful_closes_lstat_errors(unix_socket: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_lstat(_path: Path) -> object:
        raise PermissionError("private kernel detail")

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    with pytest.raises(
        DockerSocketConfigurationError, match="cannot inspect Docker socket"
    ) as caught:
        validate_docker_authority(**_rootful_kwargs(unix_socket))
    assert "private kernel detail" not in str(caught.value)


def test_socket_boundary_contains_no_identity_or_permission_mutation() -> None:
    tree = ast.parse(Path(cast(str, docker_socket_module.__file__)).read_text(encoding="utf-8"))
    forbidden = {"chmod", "chown", "setuid", "setgid"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint(forbidden)


def test_settings_require_explicit_rootless_authority() -> None:
    settings = Settings(
        sandbox_docker_mode="rootless",
        sandbox_docker_socket=None,
        sandbox_docker_transport_url=ROOTLESS_TRANSPORT_URL,
        sandbox_docker_gid=None,
    )
    assert settings.sandbox_docker_mode == "rootless"
    assert settings.sandbox_docker_transport_url == ROOTLESS_TRANSPORT_URL


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"sandbox_docker_mode": "rootless"},
        {
            "sandbox_docker_mode": "rootless",
            "sandbox_docker_transport_url": ROOTLESS_TRANSPORT_URL,
            "sandbox_docker_socket": "/run/host/docker.sock",
        },
        {
            "sandbox_docker_mode": "rootless",
            "sandbox_docker_transport_url": ROOTLESS_TRANSPORT_URL,
            "sandbox_docker_gid": 10001,
        },
        {
            "sandbox_docker_mode": "rootful",
            "sandbox_docker_socket": "relative.sock",
            "sandbox_docker_gid": 12001,
        },
        {
            "sandbox_docker_mode": "rootful",
            "sandbox_docker_socket": "/run/docker.sock",
            "sandbox_docker_gid": 0,
        },
        {
            "sandbox_docker_mode": "rootful",
            "sandbox_docker_socket": "/run/docker.sock",
            "sandbox_docker_gid": 12001,
            "sandbox_docker_transport_url": ROOTLESS_TRANSPORT_URL,
        },
    ],
)
def test_settings_reject_missing_or_cross_mode_authority(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)


def _desktop_kwargs(socket_path: Path) -> dict[str, object]:
    return {
        "mode": "desktop",
        "socket_path": socket_path,
        "transport_url": None,
        "configured_gid": None,
        "effective_uid": 10001,
        "supplemental_groups": {0},
    }


def test_desktop_accepts_only_the_root_owned_root_group_vm_socket(
    unix_socket: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: _root_owned_socket_stat(gid=0))
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    assert validate_docker_authority(**_desktop_kwargs(unix_socket)) == f"unix://{unix_socket}"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda values: values.update(effective_uid=0), "must not run as root"),
        (lambda values: values.update(effective_uid=10002), "UID 10001"),
        (lambda values: values.update(socket_path=None), "one Unix socket only"),
        (
            lambda values: values.update(transport_url=ROOTLESS_TRANSPORT_URL),
            "one Unix socket only",
        ),
        (lambda values: values.update(socket_path=Path("docker.sock")), "absolute"),
        (lambda values: values.update(configured_gid=0), "no SANDBOX_DOCKER_GID"),
        (lambda values: values.update(configured_gid=12001), "no SANDBOX_DOCKER_GID"),
        (lambda values: values.update(supplemental_groups=set()), "root group as its only"),
        (lambda values: values.update(supplemental_groups={12001}), "root group as its only"),
        (lambda values: values.update(supplemental_groups={0, 12001}), "root group as its only"),
    ],
)
def test_desktop_rejects_invalid_shape(
    unix_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: _root_owned_socket_stat(gid=0))
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    values = _desktop_kwargs(unix_socket)
    mutator(values)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("socket_stat", "access", "message"),
    [
        (_root_owned_socket_stat(gid=0, mode=stat.S_IFREG), True, "not a Unix socket"),
        (
            SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=501, st_gid=0),
            True,
            "owned by UID 0",
        ),
        (_root_owned_socket_stat(gid=12001), True, "owned by GID 0"),
        (_root_owned_socket_stat(gid=0), False, "not readable and writable"),
    ],
)
def test_desktop_rejects_socket_metadata_mismatch(
    unix_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
    socket_stat: object,
    access: bool,
    message: str,
) -> None:
    monkeypatch.setattr(Path, "lstat", lambda _path: socket_stat)
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: access)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**_desktop_kwargs(unix_socket))


def test_desktop_rejects_symlink_inside_the_container(
    unix_socket: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "docker-link.sock"
    link.symlink_to(unix_socket)
    monkeypatch.setattr(os, "access", lambda _path, _mode, *, effective_ids: True)
    with pytest.raises(DockerSocketConfigurationError, match="symlink"):
        validate_docker_authority(**_desktop_kwargs(link))


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"OperatingSystem": "Docker Desktop"}, True),
        ({"OperatingSystem": "Docker Desktop 4.x"}, True),
        ({"OperatingSystem": "Ubuntu 24.04 LTS"}, False),
        ({"OperatingSystem": 7}, False),
        ({}, False),
    ],
)
def test_daemon_desktop_identity_requires_the_exact_operating_system(
    info: dict[str, object], expected: bool
) -> None:
    assert docker_socket_module.daemon_is_docker_desktop(info) is expected


def test_settings_accept_explicit_desktop_authority() -> None:
    settings = Settings(
        sandbox_docker_mode="desktop",
        sandbox_docker_socket="/run/jhin/docker.sock",
    )
    assert settings.sandbox_docker_mode == "desktop"
    assert settings.sandbox_docker_socket == Path("/run/jhin/docker.sock")
    assert settings.sandbox_docker_gid is None
    assert settings.sandbox_docker_transport_url is None


@pytest.mark.parametrize(
    "values",
    [
        {"sandbox_docker_mode": "desktop"},
        {"sandbox_docker_mode": "desktop", "sandbox_docker_socket": "relative.sock"},
        {
            "sandbox_docker_mode": "desktop",
            "sandbox_docker_socket": "/run/jhin/docker.sock",
            "sandbox_docker_gid": 1,
        },
        {
            "sandbox_docker_mode": "desktop",
            "sandbox_docker_socket": "/run/jhin/docker.sock",
            "sandbox_docker_transport_url": ROOTLESS_TRANSPORT_URL,
        },
        {"sandbox_docker_mode": "macos", "sandbox_docker_socket": "/run/jhin/docker.sock"},
    ],
)
def test_settings_reject_incomplete_or_cross_mode_desktop_authority(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)
