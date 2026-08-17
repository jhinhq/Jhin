"""Workspace, membership, team, and agent models (plan sections 6.1-6.5).

The workspace is the security and ownership boundary: every team/agent row
carries ``workspace_id`` and every query must scope by it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, JsonList, TimestampMixin, UuidPkMixin
from jhin_domain import AgentStatus, AutonomyLevel, WorkspaceStatus


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
    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)

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
    # Explicit approval-policy rules (plan 42). Presets (Autonomous/Balanced/
    # Restricted) are a UI shortcut that expands to rows in this list; an
    # empty list means the plan 12.2 risk defaults apply.
    approval_policy_json: Mapped[list[Any]] = mapped_column(JsonList, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
