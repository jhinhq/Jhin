"""Fail-closed tests for the sandbox runner's Docker authority boundary."""

from __future__ import annotations

import ast
import os
import socket
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
    assert validate_docker_authority(**_rootful_kwargs(unix_socket)) == f"unix://{unix_socket}"

    for configured_gid, groups in [(12002, {12001}), (12001, set()), (12001, {12001, 12002})]:
        values = _rootful_kwargs(unix_socket)
        values.update(configured_gid=configured_gid, supplemental_groups=groups)
        with pytest.raises(DockerSocketConfigurationError):
            validate_docker_authority(**values)  # type: ignore[arg-type]


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
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
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
    monkeypatch.setattr(os, "access", lambda _path, _mode: access)
    with pytest.raises(DockerSocketConfigurationError, match=message):
        validate_docker_authority(**_rootful_kwargs(unix_socket))


def test_rootful_rejects_symlink_before_following_it(
    unix_socket: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "docker-link.sock"
    link.symlink_to(unix_socket)
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
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
