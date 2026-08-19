from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

CrashBarrierName = Literal[
    "phase10.agent.before_manifest_bind.v1",
    "phase9.agent.after_manifest.before_effect.v1",
    "phase9.agent.sync.before_effect.v1",
    "phase9.agent.cleanup.before_effect.v1",
    "phase10.tool.before_claim.v1",
    "phase10.tool.after_claim.before_effect.v1",
    "phase10.tool.after_effect.before_terminal_commit.v1",
]
AGENT_BEFORE_BIND: CrashBarrierName = "phase10.agent.before_manifest_bind.v1"
PHASE9_AFTER_MANIFEST: CrashBarrierName = "phase9.agent.after_manifest.before_effect.v1"
PHASE9_SYNC_BEFORE_EFFECT: CrashBarrierName = "phase9.agent.sync.before_effect.v1"
PHASE9_CLEANUP_BEFORE_EFFECT: CrashBarrierName = "phase9.agent.cleanup.before_effect.v1"
TOOL_BEFORE_CLAIM: CrashBarrierName = "phase10.tool.before_claim.v1"
TOOL_AFTER_CLAIM: CrashBarrierName = "phase10.tool.after_claim.before_effect.v1"
TOOL_AFTER_EFFECT: CrashBarrierName = "phase10.tool.after_effect.before_terminal_commit.v1"


@dataclass(frozen=True)
class CrashBarrierConfig:
    root: Path | None = None
    selected: CrashBarrierName | None = None
    match_identity: UUID | None = None


class CrashBarrier:
    def __init__(self, config: CrashBarrierConfig) -> None:
        self._config = config

    async def arrive_and_wait(self, name: CrashBarrierName, identity: UUID) -> None:
        if self._config.root is None or self._config.selected != name:
            return
        if self._config.match_identity is not None and identity != self._config.match_identity:
            return
        directory = self._config.root / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        arrived = directory / f"{identity}.arrived"
        release = directory / f"{identity}.release"
        try:
            fd = os.open(arrived, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, b"arrived\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        while not release.is_file():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


def release_barrier(root: Path, name: CrashBarrierName, identity: UUID) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / f"{identity}.release"
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"release\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
