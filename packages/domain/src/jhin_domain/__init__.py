"""Shared domain vocabulary for Jhin.

Deliberately dependency-light (stdlib + uuid-utils only) so every package —
db, events, API, workers — can depend on it without dragging in SQLAlchemy,
Pydantic, or NATS.
"""

from jhin_domain.enums import (
    APPROVAL_DECIDED_STATUSES,
    RUN_TERMINAL_STATUSES,
    SANDBOX_JOB_TERMINAL_STATUSES,
    TASK_TERMINAL_STATES,
    ActorType,
    AgentStatus,
    ApprovalStatus,
    AutonomyLevel,
    ConnectionStatus,
    MessageVisibility,
    ModelProviderType,
    RecipientType,
    RunStatus,
    SandboxJobStatus,
    SecretType,
    SenderType,
    TaskPriority,
    TaskState,
    ToolCallStatus,
    UserStatus,
    WorkspaceRole,
    WorkspaceStatus,
    role_satisfies,
)
from jhin_domain.ids import new_uuid7

__all__ = [
    "APPROVAL_DECIDED_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "SANDBOX_JOB_TERMINAL_STATUSES",
    "TASK_TERMINAL_STATES",
    "ActorType",
    "AgentStatus",
    "ApprovalStatus",
    "AutonomyLevel",
    "ConnectionStatus",
    "MessageVisibility",
    "ModelProviderType",
    "RecipientType",
    "RunStatus",
    "SandboxJobStatus",
    "SecretType",
    "SenderType",
    "TaskPriority",
    "TaskState",
    "ToolCallStatus",
    "UserStatus",
    "WorkspaceRole",
    "WorkspaceStatus",
    "new_uuid7",
    "role_satisfies",
]
