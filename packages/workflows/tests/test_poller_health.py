"""Temporal task-queue poller health contract."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.client import Client

import jhin_workflows.poller_health as poller_health


class _WorkflowService:
    def __init__(self, pollers: list[object], *, failure: Exception | None = None) -> None:
        self.pollers = pollers
        self.failure = failure
        self.requests: list[Any] = []

    async def describe_task_queue(self, request: Any) -> object:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(pollers=self.pollers)


@pytest.mark.parametrize(("pollers", "expected"), [([], False), ([object()], True)])
async def test_poller_health_uses_raw_workflow_queue_description(
    pollers: list[object],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _WorkflowService(pollers)

    async def connect(address: str, *, namespace: str) -> object:
        assert address == "temporal.internal:7233"
        assert namespace == "tenant-a"
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(Client, "connect", connect)

    assert (
        await poller_health.queue_has_workflow_poller(
            "temporal.internal:7233", "tenant-a", "jhin-tool-queue"
        )
        is expected
    )
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.namespace == "tenant-a"
    assert request.task_queue.name == "jhin-tool-queue"
    assert request.task_queue_type == TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW


@pytest.mark.parametrize(
    ("result", "expected_code", "expected_output"),
    [
        (True, 0, "workflow-poller-ready\n"),
        (False, 1, "workflow-poller-unavailable\n"),
    ],
)
async def test_cli_reads_exact_environment_and_emits_closed_status(
    result: bool,
    expected_code: int,
    expected_output: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal.private:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "private-namespace")

    async def check(address: str, namespace: str, queue: str) -> bool:
        assert (address, namespace, queue) == (
            "temporal.private:7233",
            "private-namespace",
            "jhin-agent-queue",
        )
        return result

    monkeypatch.setattr(poller_health, "queue_has_workflow_poller", check)

    assert await poller_health.main("jhin-agent-queue") == expected_code
    assert capsys.readouterr() == (expected_output, "")


@pytest.mark.parametrize("failure_stage", ["connect", "rpc"])
async def test_cli_maps_temporal_failures_to_one_closed_output(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "do-not-print.example:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "do-not-print-namespace")
    service = _WorkflowService([], failure=RuntimeError("do-not-print-rpc"))

    async def connect(_address: str, *, namespace: str) -> object:
        assert namespace == "do-not-print-namespace"
        if failure_stage == "connect":
            raise RuntimeError("do-not-print-connect")
        return SimpleNamespace(workflow_service=service)

    monkeypatch.setattr(Client, "connect", connect)

    assert await poller_health.main("do-not-print-queue") == 1
    captured = capsys.readouterr()
    assert captured == ("workflow-poller-unavailable\n", "")
    assert "do-not-print" not in captured.out + captured.err
