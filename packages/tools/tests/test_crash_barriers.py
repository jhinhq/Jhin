from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from jhin_tools import (
    PHASE9_AFTER_MANIFEST,
    TOOL_AFTER_CLAIM,
    CrashBarrier,
    CrashBarrierConfig,
    release_barrier,
)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,  # noqa: ASYNC109
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_barrier_fsyncs_arrival_and_waits_for_release(tmp_path: Path) -> None:
    barrier = CrashBarrier(CrashBarrierConfig(root=tmp_path, selected=TOOL_AFTER_CLAIM))
    identity = UUID("018f4d52-8b93-7d41-8ac7-7f190f091111")
    waiting = asyncio.create_task(barrier.arrive_and_wait(TOOL_AFTER_CLAIM, identity))
    arrived = tmp_path / TOOL_AFTER_CLAIM / f"{identity}.arrived"
    release = tmp_path / TOOL_AFTER_CLAIM / f"{identity}.release"
    await wait_until(arrived.exists)
    assert arrived.read_bytes() == b"arrived\n"
    assert stat.S_IMODE(arrived.stat().st_mode) == 0o644
    assert not waiting.done()
    release_barrier(tmp_path, TOOL_AFTER_CLAIM, identity)
    assert release.read_bytes() == b"release\n"
    await asyncio.wait_for(waiting, timeout=1)


async def test_unconfigured_barrier_is_a_no_op(tmp_path: Path) -> None:
    identity = UUID("018f4d52-8b93-7d41-8ac7-7f190f091111")

    await asyncio.wait_for(
        CrashBarrier(CrashBarrierConfig()).arrive_and_wait(TOOL_AFTER_CLAIM, identity),
        timeout=0.1,
    )

    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240


@pytest.mark.parametrize("mismatch", ["name", "identity"])
async def test_barrier_ignores_unselected_arrivals(tmp_path: Path, mismatch: str) -> None:
    identity = UUID("018f4d52-8b93-7d41-8ac7-7f190f091111")
    configured_identity = (
        UUID("018f4d52-8b93-7d41-8ac7-7f190f092222") if mismatch == "identity" else identity
    )
    selected = PHASE9_AFTER_MANIFEST if mismatch == "name" else TOOL_AFTER_CLAIM
    barrier = CrashBarrier(
        CrashBarrierConfig(
            root=tmp_path,
            selected=selected,
            match_identity=configured_identity,
        )
    )

    await asyncio.wait_for(barrier.arrive_and_wait(TOOL_AFTER_CLAIM, identity), timeout=0.1)

    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240
