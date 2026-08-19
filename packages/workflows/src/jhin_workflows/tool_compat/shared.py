"""Dependency-light contracts for Phase 9 effect compatibility workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from jhin_workflows.agent_task.shared import ResolveBoundToolApprovalInput

CompatibilityKind = Literal["advertised", "tool-step", "approval", "sync", "cleanup"]


@dataclass
class AdvertisedCompatibilityInput:
    workspace_id: str
    agent_id: str


@dataclass
class ToolStepCompatibilityInput:
    workspace_id: str
    run_id: str
    step_index: int
    call_count: int


@dataclass
class ApprovalCompatibilityInput(ResolveBoundToolApprovalInput):
    pass


@dataclass
class SyncExternalToolInput:
    workspace_id: str
    task_id: str
    run_id: str


def compatibility_workflow_id(
    kind: CompatibilityKind,
    identity: str,
    *,
    step_index: int | None = None,
) -> str:
    """Return one canonical reattachment key from a durable UUID identity."""
    suffix = f"-{step_index}" if step_index is not None else ""
    return f"phase10-compat-{kind}-{UUID(identity)}{suffix}"


__all__ = [
    "AdvertisedCompatibilityInput",
    "ApprovalCompatibilityInput",
    "CompatibilityKind",
    "SyncExternalToolInput",
    "ToolStepCompatibilityInput",
    "compatibility_workflow_id",
]
