"""Canonical enumerations shared across packages (plan section 6).

Stored as plain strings in Postgres (no native enum types) so adding a value
never requires a migration; validity is enforced at the API boundary.
"""

from enum import StrEnum


class WorkspaceRole(StrEnum):
    """Workspace RBAC roles, weakest to strongest (plan 20.2)."""

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


_ROLE_ORDER: dict[WorkspaceRole, int] = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


def role_satisfies(actual: WorkspaceRole, required: WorkspaceRole) -> bool:
    """True when ``actual`` grants at least the privileges of ``required``."""
    return _ROLE_ORDER[actual] >= _ROLE_ORDER[required]


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class AutonomyLevel(StrEnum):
    """How much an agent may do without human sign-off.

    Enforcement arrives with the policy engine (Phase 4); Phase 2 only stores
    the configuration.
    """

    MANUAL = "manual"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class ActorType(StrEnum):
    """Who performed an audited action (plan 6.17)."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class SecretType(StrEnum):
    """What kind of credential an encrypted secret holds (plan 6.10)."""

    API_KEY = "api_key"
    TOKEN = "token"
    PASSWORD = "password"
    OTHER = "other"


class ModelProviderType(StrEnum):
    """Supported model provider adapters (plan 6.7, 15.1)."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class TaskState(StrEnum):
    """Lifecycle of user-visible work (plan 6.12)."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RunStatus(StrEnum):
    """Lifecycle of a single agent run (plan 6.13)."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class SenderType(StrEnum):
    """Who authored a message (plan 6.14)."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RecipientType(StrEnum):
    USER = "user"
    AGENT = "agent"
    TEAM = "team"
    TASK = "task"


class MessageVisibility(StrEnum):
    """Whether a message is shown in the product UI or internal-only."""

    VISIBLE = "visible"
    INTERNAL = "internal"
