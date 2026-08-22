"""Stable workflow wrappers used by Phase 9 compatibility activities."""

from jhin_workflows.tool_compat.shared import (
    AdvertisedCompatibilityInput,
    ApprovalCompatibilityInput,
    CompatibilityKind,
    SyncExternalToolInput,
    ToolStepCompatibilityInput,
    compatibility_workflow_id,
)
from jhin_workflows.tool_compat.workflows import (
    AdvertisedToolsCompatibilityWorkflow,
    ApprovalCompatibilityWorkflow,
    CleanupCompatibilityWorkflow,
    SyncExternalCompatibilityWorkflow,
    ToolStepCompatibilityWorkflow,
)

__all__ = [
    "AdvertisedCompatibilityInput",
    "AdvertisedToolsCompatibilityWorkflow",
    "ApprovalCompatibilityInput",
    "ApprovalCompatibilityWorkflow",
    "CleanupCompatibilityWorkflow",
    "CompatibilityKind",
    "SyncExternalCompatibilityWorkflow",
    "SyncExternalToolInput",
    "ToolStepCompatibilityInput",
    "ToolStepCompatibilityWorkflow",
    "compatibility_workflow_id",
]
