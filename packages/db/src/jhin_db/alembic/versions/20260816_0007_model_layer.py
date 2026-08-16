"""Model providers and profiles; wire workspace/agent references (plan 6.7, 6.8).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_provider",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("secret_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_provider"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_model_provider_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["secret_id"],
            ["secret.id"],
            name="fk_model_provider_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("workspace_id", "display_name", name="uq_model_provider_workspace_id"),
    )
    op.create_index("ix_model_provider_workspace_id", "model_provider", ["workspace_id"])

    op.create_table(
        "model_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("input_cost_micros_per_million", sa.Integer(), nullable=True),
        sa.Column("output_cost_micros_per_million", sa.Integer(), nullable=True),
        sa.Column("supports_tools", sa.Boolean(), nullable=False),
        sa.Column("supports_reasoning", sa.Boolean(), nullable=False),
        sa.Column("config_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_profile"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_model_profile_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["model_provider.id"],
            name="fk_model_profile_provider_id_model_provider",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "display_name", name="uq_model_profile_workspace_id"),
    )
    op.create_index("ix_model_profile_workspace_id", "model_profile", ["workspace_id"])
    op.create_index("ix_model_profile_provider_id", "model_profile", ["provider_id"])

    op.add_column("workspace", sa.Column("default_model_profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_workspace_default_model_profile",
        "workspace",
        "model_profile",
        ["default_model_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # agent.model_profile_id existed as a bare UUID since Phase 2; attach FK.
    op.create_foreign_key(
        "fk_agent_model_profile",
        "agent",
        "model_profile",
        ["model_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_model_profile", "agent", type_="foreignkey")
    op.drop_constraint("fk_workspace_default_model_profile", "workspace", type_="foreignkey")
    op.drop_column("workspace", "default_model_profile_id")
    op.drop_table("model_profile")
    op.drop_table("model_provider")
