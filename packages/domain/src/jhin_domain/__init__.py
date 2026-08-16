"""Shared domain vocabulary for Jhin.

Deliberately dependency-light (stdlib + uuid-utils only) so every package —
db, events, API, workers — can depend on it without dragging in SQLAlchemy,
Pydantic, or NATS.
"""

from jhin_domain.enums import (
    RUN_TERMINAL_STATUSES,
    TASK_TERMINAL_STATES,
    ActorType,
    AgentStatus,
    AutonomyLevel,
    MessageVisibility,
    ModelProviderType,
    RecipientType,
    RunStatus,
    SecretType,
    SenderType,
    TaskPriority,
    TaskState,
    UserStatus,
    WorkspaceRole,
    WorkspaceStatus,
    role_satisfies,
)
from jhin_domain.ids import new_uuid7

__all__ = [
    "RUN_TERMINAL_STATUSES",
    "TASK_TERMINAL_STATES",
    "ActorType",
    "AgentStatus",
    "AutonomyLevel",
    "MessageVisibility",
    "ModelProviderType",
    "RecipientType",
    "RunStatus",
    "SecretType",
    "SenderType",
    "TaskPriority",
    "TaskState",
    "UserStatus",
    "WorkspaceRole",
    "WorkspaceStatus",
    "new_uuid7",
    "role_satisfies",
]
