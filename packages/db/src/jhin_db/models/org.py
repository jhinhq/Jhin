"""Workspace, membership, team, and agent models (plan sections 6.1-6.5).

The workspace is the security and ownership boundary: every team/agent row
carries ``workspace_id`` and every query must scope by it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from jhin_db.base import Base
from jhin_db.columns import JsonDict, JsonList, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import AgentStatus, AutonomyLevel, WorkspaceStatus

MAX_EXPERTISE_TAGS = 20
MAX_EXPERTISE_TAG_LENGTH = 64


class Workspace(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "workspace"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(32), default=WorkspaceStatus.ACTIVE.value)
    # use_alter: workspace <-> model_profile reference each other.
    default_model_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "model_profile.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_workspace_default_model_profile",
        ),
        default=None,
    )
    default_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    settings_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)


class WorkspaceMembership(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "workspace_membership"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))


class Team(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "team"
    __table_args__ = (UniqueConstraint("workspace_id", "id", name="uq_team_workspace_id_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    parent_team_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("team.id", ondelete="SET NULL"), default=None
    )
    # use_alter breaks the team <-> agent circular foreign-key dependency.
    manager_agent_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("agent.id", ondelete="SET NULL", use_alter=True, name="fk_team_manager_agent"),
        default=None,
    )
    color_token: Mapped[str] = mapped_column(String(32), default="slate")
    icon: Mapped[str] = mapped_column(String(64), default="users")


class Agent(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "agent"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),
        UniqueConstraint("workspace_id", "id", name="uq_agent_workspace_id_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("team.id", ondelete="SET NULL"), default=None
    )
    manager_agent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    role_title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=AgentStatus.ACTIVE.value)
    autonomy_level: Mapped[str] = mapped_column(String(32), default=AutonomyLevel.SUPERVISED.value)
    # Null means "use the workspace default profile" (plan 15.2).
    model_profile_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "model_profile.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_agent_model_profile",
        ),
        default=None,
    )
    temperature: Mapped[float | None] = mapped_column(Float, default=None)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    max_steps: Mapped[int] = mapped_column(Integer, default=20)
    max_run_minutes: Mapped[int] = mapped_column(Integer, default=30)
    # Concurrency admission (plan 30): active runs beyond this queue visibly
    # instead of starting. 1 = one coding ticket at a time.
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    monthly_budget_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    public_purpose: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    expertise_json: Mapped[list[str]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    discoverability: Mapped[str] = mapped_column(
        String(32), default="discoverable", server_default=text("'discoverable'")
    )
    availability: Mapped[str] = mapped_column(
        String(32), default="available", server_default=text("'available'")
    )
    # Explicit approval-policy rules (plan 42). Presets (Autonomous/Balanced/
    # Restricted) are a UI shortcut that expands to rows in this list; an
    # empty list means the plan 12.2 risk defaults apply.
    approval_policy_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)

    @validates("expertise_json")
    def _validate_expertise_json(self, _key: str, value: list[str]) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("expertise_json must be a JSON array of strings")
        if len(value) > MAX_EXPERTISE_TAGS:
            raise ValueError(f"expertise_json accepts at most {MAX_EXPERTISE_TAGS} tags")
        if any(not isinstance(tag, str) for tag in value):
            raise ValueError("expertise_json tags must be strings")
        if any(not 1 <= len(tag) <= MAX_EXPERTISE_TAG_LENGTH for tag in value):
            raise ValueError(
                f"expertise_json tags must contain 1 to {MAX_EXPERTISE_TAG_LENGTH} characters"
            )
        return value


class AgentTeamMembership(Base, UuidPkMixin):
    """A workspace-local team association; membership never grants authority."""

    __tablename__ = "agent_team_membership"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_team_membership_workspace_agent",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "team_id"],
            ["team.workspace_id", "team.id"],
            name="fk_agent_team_membership_workspace_team",
            ondelete="CASCADE",
        ),
        Index(
            "uq_agent_team_membership_active_primary",
            "workspace_id",
            "agent_id",
            unique=True,
            postgresql_where=text("left_at IS NULL AND is_primary"),
            sqlite_where=text("left_at IS NULL AND is_primary"),
        ),
        Index(
            "uq_agent_team_membership_active_pair",
            "workspace_id",
            "agent_id",
            "team_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
            sqlite_where=text("left_at IS NULL"),
        ),
        Index(
            "ix_agent_team_membership_workspace_agent",
            "workspace_id",
            "agent_id",
        ),
        Index(
            "ix_agent_team_membership_workspace_team",
            "workspace_id",
            "team_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspace.id", ondelete="CASCADE"))
    agent_id: Mapped[UUID] = mapped_column(Uuid)
    team_id: Mapped[UUID] = mapped_column(Uuid)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    role_label: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    joined_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class AgentRelationship(Base, UuidPkMixin, TimestampMixin):
    """Workspace-local routing context; relationships never grant authority."""

    __tablename__ = "agent_relationship"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_relationship_workspace_source_agent",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "target_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_agent_relationship_workspace_target_agent",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('close_collaborator', 'advisor', 'preferred_reviewer')",
            name="kind",
        ),
        CheckConstraint(
            "kind <> 'close_collaborator' OR source_agent_id < target_agent_id",
            name="close_collaborator_order",
        ),
        CheckConstraint(
            "kind = 'close_collaborator' OR source_agent_id <> target_agent_id",
            name="directed_not_self",
        ),
        CheckConstraint("status IN ('active', 'inactive')", name="status"),
        Index(
            "uq_agent_relationship_active_pair_kind",
            "workspace_id",
            "source_agent_id",
            "target_agent_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ix_agent_relationship_workspace_source",
            "workspace_id",
            "source_agent_id",
        ),
        Index(
            "ix_agent_relationship_workspace_target",
            "workspace_id",
            "target_agent_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspace.id", ondelete="CASCADE"))
    source_agent_id: Mapped[UUID] = mapped_column(Uuid)
    target_agent_id: Mapped[UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default=text("'active'")
    )
