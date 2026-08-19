"""Fail-closed validation for the runner's sole Docker authority."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

DockerSocketMode = Literal["rootless", "rootful"]
ROOTLESS_TRANSPORT_URL = "http://rootless-docker-transport:2375"


class DockerSocketConfigurationError(RuntimeError):
    """The configured Docker authority does not match the selected mode."""


def normalize_supplemental_groups(*, effective_gid: int, process_groups: Iterable[int]) -> set[int]:
    """Return group authorities other than the process's primary group."""
    return set(process_groups) - {effective_gid}


def validate_docker_authority(
    *,
    mode: DockerSocketMode,
    socket_path: Path | None,
    transport_url: str | None,
    configured_gid: int | None,
    effective_uid: int,
    supplemental_groups: set[int],
) -> str:
    """Return the only aiodocker URL the runner is authorized to use."""
    if mode not in ("rootless", "rootful"):
        raise DockerSocketConfigurationError("unsupported Docker socket mode")
    if effective_uid == 0:
        raise DockerSocketConfigurationError("sandbox runner must not run as root")

    if mode == "rootless":
        if effective_uid != 10001:
            raise DockerSocketConfigurationError("rootless runner requires UID 10001")
        if socket_path is not None:
            raise DockerSocketConfigurationError("rootless runner requires no socket mount")
        if configured_gid is not None:
            raise DockerSocketConfigurationError("rootless runner requires no socket GID")
        if supplemental_groups:
            raise DockerSocketConfigurationError("rootless runner requires no supplemental groups")
        if transport_url != ROOTLESS_TRANSPORT_URL:
            raise DockerSocketConfigurationError(
                "rootless transport URL is not the private endpoint"
            )
        return ROOTLESS_TRANSPORT_URL

    if socket_path is None or transport_url is not None:
        raise DockerSocketConfigurationError("rootful runner requires one Unix socket only")
    if not socket_path.is_absolute():
        raise DockerSocketConfigurationError("rootful Docker socket path must be absolute")
    if configured_gid is None or configured_gid <= 0:
        raise DockerSocketConfigurationError(
            "rootful runner requires a positive SANDBOX_DOCKER_GID"
        )
    try:
        info = socket_path.lstat()
    except OSError as exc:
        raise DockerSocketConfigurationError("cannot inspect Docker socket") from exc
    if stat.S_ISLNK(info.st_mode):
        raise DockerSocketConfigurationError("configured Docker endpoint must not be a symlink")
    if not stat.S_ISSOCK(info.st_mode):
        raise DockerSocketConfigurationError("configured Docker endpoint is not a Unix socket")
    if info.st_uid != 0:
        raise DockerSocketConfigurationError("Docker socket must be owned by UID 0")
    if info.st_gid != configured_gid:
        raise DockerSocketConfigurationError(
            "Docker socket group does not match SANDBOX_DOCKER_GID"
        )
    if supplemental_groups != {configured_gid}:
        raise DockerSocketConfigurationError("runner requires the exact Docker socket group only")
    if not os.access(socket_path, os.R_OK | os.W_OK, effective_ids=True):
        raise DockerSocketConfigurationError(
            "Docker socket is not readable and writable by the runner"
        )
    return f"unix://{socket_path}"
