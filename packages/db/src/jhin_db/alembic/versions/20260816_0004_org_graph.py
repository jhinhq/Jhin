"""Teams and agents — the organization graph (plan 6.4, 6.5).

The team.manager_agent_id foreign key is added after both tables exist
because team and agent reference each other.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_team_id", sa.Uuid(), nullable=True),
        sa.Column("manager_agent_id", sa.Uuid(), nullable=True),
        sa.Column("color_token", sa.String(32), nullable=False),
        sa.Column("icon", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_team"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_team_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_team_id"], ["team.id"], name="fk_team_parent_team_id_team", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_team_workspace_id", "team", ["workspace_id"])

    op.create_table(
        "agent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("manager_agent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("role_title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("autonomy_level", sa.String(32), nullable=False),
        sa.Column("model_profile_id", sa.Uuid(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("max_run_minutes", sa.Integer(), nullable=False),
        sa.Column("monthly_budget_cents", sa.Integer(), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name="fk_agent_team_id_team", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["manager_agent_id"],
            ["agent.id"],
            name="fk_agent_manager_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_agent_workspace_id"),
    )
    op.create_index("ix_agent_workspace_id", "agent", ["workspace_id"])

    op.create_foreign_key(
        "fk_team_manager_agent",
        "team",
        "agent",
        ["manager_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_team_manager_agent", "team", type_="foreignkey")
    op.drop_table("agent")
    op.drop_table("team")
