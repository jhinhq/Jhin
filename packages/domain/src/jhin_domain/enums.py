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
