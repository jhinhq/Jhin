"""PeriodicReviewWorkflow with stub activities under time skipping: one
review per epoch-aligned window, idempotent window keys, stop/refresh
signals, and exit on disabled or deleted policies."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jhin_workflows.periodic_review import (
    ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY,
    ACTIVITY_OPEN_PERIODIC_REVIEW,
    SIGNAL_PERIODIC_REVIEW_REFRESH,
    SIGNAL_PERIODIC_REVIEW_STOP,
    OpenPeriodicReviewInput,
    OpenPeriodicReviewResult,
    PeriodicReviewInput,
    PeriodicReviewPolicyState,
    PeriodicReviewResult,
    PeriodicReviewWorkflow,
    periodic_review_workflow_id,
    window_bounds,
)


class Stubs:
    def __init__(self, *, enabled: bool = True, period_seconds: int = 3600) -> None:
        self.state = PeriodicReviewPolicyState(
            exists=True, enabled=enabled, period_seconds=period_seconds
        )
        self.loads = 0
        self.opened: list[OpenPeriodicReviewInput] = []
        self.reviews: dict[str, str] = {}

    @activity.defn(name=ACTIVITY_LOAD_PERIODIC_REVIEW_POLICY)
    async def load(self, _params: PeriodicReviewInput) -> PeriodicReviewPolicyState:
        self.loads += 1
        return self.state

    @activity.defn(name=ACTIVITY_OPEN_PERIODIC_REVIEW)
    async def open(self, params: OpenPeriodicReviewInput) -> OpenPeriodicReviewResult:
        self.opened.append(params)
        key = f"{params.policy_id}:{params.window_start}"
        created = key not in self.reviews
        self.reviews.setdefault(key, str(uuid.uuid4()))
        return OpenPeriodicReviewResult(
            review_id=self.reviews[key], status="pending", created=created
        )


@pytest.fixture
async def env() -> Any:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _params() -> PeriodicReviewInput:
    return PeriodicReviewInput(workspace_id=str(uuid.uuid4()), policy_id=str(uuid.uuid4()))


async def _worker(env: WorkflowEnvironment, stubs: Stubs, queue: str) -> Worker:
    return Worker(
        env.client,
        task_queue=queue,
        workflows=[PeriodicReviewWorkflow],
        activities=[stubs.load, stubs.open],
    )


def test_windows_are_epoch_aligned_and_deterministic() -> None:
    now = datetime(2026, 8, 22, 10, 17, 3, tzinfo=UTC)
    start, end = window_bounds(now, timedelta(hours=1))
    assert start == datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 22, 11, 0, tzinfo=UTC)
    assert window_bounds(start, timedelta(hours=1)) == (start, end)


async def test_opens_one_review_per_window_then_stops(env: Any) -> None:
    stubs = Stubs(period_seconds=3600)
    params = _params()
    queue = f"periodic-{uuid.uuid4()}"
    async with await _worker(env, stubs, queue):
        handle = await env.client.start_workflow(
            PeriodicReviewWorkflow.run,
            params,
            id=periodic_review_workflow_id(params.policy_id),
            task_queue=queue,
        )
        for _ in range(200):
            await env.sleep(timedelta(minutes=20))
            if len(stubs.opened) >= 3:
                break
        await handle.signal(SIGNAL_PERIODIC_REVIEW_STOP)
        result: PeriodicReviewResult = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.reason == "stopped" and result.windows_done >= 3
    starts = [o.window_start for o in stubs.opened]
    assert len(starts) == len(set(starts)), "each window opens exactly one review"
    for opened in stubs.opened:
        begin = datetime.strptime(opened.window_start, "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(opened.window_end, "%Y-%m-%dT%H:%M:%SZ")
        assert end - begin == timedelta(hours=1)
        assert begin.minute == 0 and begin.second == 0
    assert all(o.policy_id == params.policy_id for o in stubs.opened)


async def test_disabled_policy_exits_without_opening(env: Any) -> None:
    stubs = Stubs(enabled=False)
    params = _params()
    queue = f"periodic-{uuid.uuid4()}"
    async with await _worker(env, stubs, queue):
        result: PeriodicReviewResult = await env.client.execute_workflow(
            PeriodicReviewWorkflow.run,
            params,
            id=periodic_review_workflow_id(params.policy_id),
            task_queue=queue,
        )
    assert result.reason == "disabled" and stubs.opened == []


async def test_deleted_policy_exits_before_the_next_window(env: Any) -> None:
    stubs = Stubs(period_seconds=3600)
    params = _params()
    queue = f"periodic-{uuid.uuid4()}"
    async with await _worker(env, stubs, queue):
        handle = await env.client.start_workflow(
            PeriodicReviewWorkflow.run,
            params,
            id=periodic_review_workflow_id(params.policy_id),
            task_queue=queue,
        )
        for _ in range(100):
            await env.sleep(timedelta(minutes=30))
            if stubs.opened:
                break
        stubs.state = PeriodicReviewPolicyState(exists=False, enabled=False, period_seconds=0)
        await env.sleep(timedelta(hours=2))
        result: PeriodicReviewResult = await asyncio.wait_for(handle.result(), timeout=10)
    assert result.reason == "deleted"
    assert len(stubs.opened) >= 1


async def test_refresh_reloads_the_cadence_without_opening_a_stale_window(env: Any) -> None:
    stubs = Stubs(period_seconds=24 * 3600)
    params = _params()
    queue = f"periodic-{uuid.uuid4()}"
    async with await _worker(env, stubs, queue):
        handle = await env.client.start_workflow(
            PeriodicReviewWorkflow.run,
            params,
            id=periodic_review_workflow_id(params.policy_id),
            task_queue=queue,
        )
        await env.sleep(timedelta(minutes=5))
        loads_before = stubs.loads
        stubs.state = PeriodicReviewPolicyState(exists=True, enabled=True, period_seconds=3600)
        await handle.signal(SIGNAL_PERIODIC_REVIEW_REFRESH)
        for _ in range(100):
            await env.sleep(timedelta(minutes=30))
            if len(stubs.opened) >= 2:
                break
        await handle.signal(SIGNAL_PERIODIC_REVIEW_STOP)
        await asyncio.wait_for(handle.result(), timeout=10)
    assert stubs.loads > loads_before
    assert len(stubs.opened) >= 2
    first = datetime.strptime(stubs.opened[0].window_start, "%Y-%m-%dT%H:%M:%SZ")
    second = datetime.strptime(stubs.opened[0].window_end, "%Y-%m-%dT%H:%M:%SZ")
    assert second - first == timedelta(hours=1), "windows follow the refreshed cadence"
