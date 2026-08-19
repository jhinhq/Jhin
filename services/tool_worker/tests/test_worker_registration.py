"""Final agent/tool worker ownership and registration contracts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

import jhin_agent_worker.activities as agent_activities_module
import jhin_agent_worker.main as agent_main
import jhin_tool_worker.main as tool_main
from jhin_agent_worker.activities import AgentActivities
from jhin_agent_worker.compatibility import AgentCompatibilityActivities
from jhin_agent_worker.projections import _cancel_pending_run_approvals
from jhin_agent_worker.trigger_activities import TriggerCompatibilityActivities
from jhin_tool_worker.activities import ToolActivities
from jhin_tool_worker.cleanup_activities import CleanupActivities
from jhin_tool_worker.settings import ToolWorkerSettings
from jhin_tool_worker.trigger_activities import TriggerToolActivities
from jhin_tools import ToolCatalog
from jhin_workflows.agent_task import AgentTaskWorkflow
from jhin_workflows.delegated_task import DelegatedTaskWorkflow
from jhin_workflows.engineering_ticket import EngineeringTicketWorkflow
from jhin_workflows.tool_compat import (
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
)
from jhin_workflows.triggered_task import TriggeredTaskWorkflow

TOOL_ACTIVITY_NAMES = {
    "resolve_advertised_tools",
    "execute_bound_tool",
    "resolve_bound_tool_approval",
    "sync_external_tool",
    "cleanup_run_workspace",
}

AGENT_ACTIVITY_NAMES = {
    "resolve_snapshot",
    "reason_agent_step",
    "commit_agent_step",
    "commit_approval_projection",
    "finalize_run_projection",
    "summarize_delegation",
    "deliver_delegation_result",
    "prepare_triggered_task",
    "resolve_engineering_plan",
    "create_engineering_child_task",
    "finalize_engineering_ticket",
    "run_agent_step",
    "resolve_approval",
    "finalize_run",
    "sync_external",
}


class _Resources:
    def __init__(self) -> None:
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class _ImmediateEvent:
    def set(self) -> None:
        return None

    async def wait(self) -> None:
        return None


class _SignalLoop:
    def add_signal_handler(self, *_args: object) -> None:
        return None


def _activity_map(activities: list[Callable[..., Any]]) -> dict[str, Any]:
    registered: dict[str, Any] = {}
    for registered_activity in activities:
        definition = getattr(registered_activity, "__temporal_activity_definition", None)
        assert definition is not None
        assert definition.name not in registered
        registered[definition.name] = registered_activity
    return registered


async def _capture_agent_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _Resources]:
    captured: dict[str, Any] = {}
    resources = _Resources()

    class _Worker:
        def __init__(self, *_args: object, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> _Worker:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def connect_with_retry(_settings: object) -> object:
        return object()

    async def resources_with_retry(_settings: object) -> _Resources:
        return resources

    async def completed_heartbeat() -> None:
        return None

    monkeypatch.setattr(agent_main, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(agent_main, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(agent_main, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_main, "run_heartbeat", completed_heartbeat)
    monkeypatch.setattr(agent_main, "clear_heartbeat", lambda: None)
    monkeypatch.setattr(agent_main, "Worker", _Worker)
    monkeypatch.setattr(asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(asyncio, "get_running_loop", _SignalLoop)

    await agent_main.main()
    return captured, resources


async def _capture_tool_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], _Resources]:
    captured: dict[str, Any] = {}
    resources = _Resources()

    class _Worker:
        def __init__(self, *_args: object, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> _Worker:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def connect_with_retry(_settings: ToolWorkerSettings) -> object:
        return object()

    async def resources_with_retry(_settings: ToolWorkerSettings) -> _Resources:
        return resources

    monkeypatch.setattr(tool_main, "connect_with_retry", connect_with_retry)
    monkeypatch.setattr(tool_main, "resources_with_retry", resources_with_retry)
    monkeypatch.setattr(tool_main, "build_default_catalog", ToolCatalog)
    monkeypatch.setattr(tool_main, "configure_current_logging", lambda _level: None)
    monkeypatch.setattr(tool_main, "Worker", _Worker)
    monkeypatch.setattr(asyncio, "Event", _ImmediateEvent)
    monkeypatch.setattr(asyncio, "get_running_loop", _SignalLoop)

    await tool_main.main()
    return captured, resources


async def test_agent_worker_registration_uses_only_agent_and_legacy_coordinators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, resources = await _capture_agent_registration(monkeypatch)

    assert set(captured["workflows"]) == {
        AgentTaskWorkflow,
        TriggeredTaskWorkflow,
        DelegatedTaskWorkflow,
        EngineeringTicketWorkflow,
    }
    registered = _activity_map(captured["activities"])
    assert set(registered) == AGENT_ACTIVITY_NAMES
    assert set(registered).isdisjoint(TOOL_ACTIVITY_NAMES)
    for name in ("run_agent_step", "resolve_approval", "finalize_run"):
        assert isinstance(registered[name].__self__, AgentCompatibilityActivities)
    assert isinstance(
        registered["sync_external"].__self__,
        TriggerCompatibilityActivities,
    )
    assert resources.close_count == 1


def test_agent_activity_class_no_longer_defines_legacy_effect_handlers() -> None:
    assert "run_agent_step_activity" not in AgentActivities.__dict__
    assert "resolve_approval_activity" not in AgentActivities.__dict__
    assert "finalize_run_activity" not in AgentActivities.__dict__


def test_legacy_approval_helper_is_a_projection_reexport() -> None:
    assert agent_activities_module._cancel_pending_run_approvals is _cancel_pending_run_approvals


async def test_tool_worker_registration_is_exactly_the_effect_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, resources = await _capture_tool_registration(monkeypatch)

    assert set(captured["workflows"]) == {
        AdvertisedToolsCompatibilityWorkflow,
        ToolStepCompatibilityWorkflow,
        ApprovalCompatibilityWorkflow,
        SyncExternalCompatibilityWorkflow,
        CleanupCompatibilityWorkflow,
    }
    registered = _activity_map(captured["activities"])
    assert set(registered) == TOOL_ACTIVITY_NAMES
    assert isinstance(registered["resolve_advertised_tools"].__self__, ToolActivities)
    assert isinstance(registered["execute_bound_tool"].__self__, ToolActivities)
    assert isinstance(registered["resolve_bound_tool_approval"].__self__, ToolActivities)
    assert isinstance(registered["sync_external_tool"].__self__, TriggerToolActivities)
    assert isinstance(registered["cleanup_run_workspace"].__self__, CleanupActivities)
    assert resources.close_count == 1


def test_tool_worker_uses_current_dependency_free_logging_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[dict[str, object]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: configured.append(kwargs))

    tool_main.configure_current_logging("warning")

    assert configured == [
        {
            "level": "WARNING",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        }
    ]
