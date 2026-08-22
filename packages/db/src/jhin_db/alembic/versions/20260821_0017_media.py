"""Agent avatars and media: ``media_asset`` (normalized WebP variants stored
inline), ``avatar_generation`` (asynchronous stylized avatar jobs), and the
additive ``agent.avatar_kind`` / ``agent.active_avatar_asset_id`` columns.

No backfill: every existing agent keeps ``avatar_kind = 'initials'``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_asset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default=sa.text("'avatar'")),
        sa.Column("owner_agent_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "content_type",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'image/webp'"),
        ),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("variant_64", sa.LargeBinary(), nullable=False),
        sa.Column("variant_128", sa.LargeBinary(), nullable=False),
        sa.Column("variant_256", sa.LargeBinary(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_media_asset"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_media_asset_workspace_id_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_media_asset_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_media_asset_owner_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_media_asset_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'retired')",
            name="ck_media_asset_status_values",
        ),
        sa.CheckConstraint("kind IN ('avatar')", name="ck_media_asset_kind_values"),
    )
    op.create_index("ix_media_asset_workspace_id", "media_asset", ["workspace_id"])
    op.create_index(
        "ix_media_asset_workspace_owner", "media_asset", ["workspace_id", "owner_agent_id"]
    )

    op.create_table(
        "avatar_generation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("prompt_hint", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("provider_display_name", sa.String(200), nullable=False),
        sa.Column("model_profile_id", sa.Uuid(), nullable=True),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column(
            "image_size", sa.String(16), nullable=False, server_default=sa.text("'1024x1024'")
        ),
        sa.Column("estimated_cost_micros", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("result_asset_id", sa.Uuid(), nullable=True),
        sa.Column("temporal_workflow_id", sa.String(200), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_avatar_generation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_avatar_generation_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="fk_avatar_generation_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profile.id"],
            name="fk_avatar_generation_model_profile_id_model_profile",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_asset_id"],
            ["media_asset.id"],
            name="fk_avatar_generation_result_asset_id_media_asset",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_avatar_generation_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_avatar_generation_status_values",
        ),
    )
    op.create_index("ix_avatar_generation_workspace_id", "avatar_generation", ["workspace_id"])
    op.create_index(
        "ix_avatar_generation_workspace_agent",
        "avatar_generation",
        ["workspace_id", "agent_id"],
    )

    op.add_column(
        "agent",
        sa.Column(
            "avatar_kind", sa.String(16), nullable=False, server_default=sa.text("'initials'")
        ),
    )
    op.add_column("agent", sa.Column("active_avatar_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_agent_active_avatar_asset",
        "agent",
        "media_asset",
        ["active_avatar_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_agent_avatar_kind_values",
        "agent",
        "avatar_kind IN ('initials', 'upload', 'generated')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_avatar_kind_values", "agent", type_="check")
    op.drop_constraint("fk_agent_active_avatar_asset", "agent", type_="foreignkey")
    op.drop_column("agent", "active_avatar_asset_id")
    op.drop_column("agent", "avatar_kind")
    op.drop_index("ix_avatar_generation_workspace_agent", table_name="avatar_generation")
    op.drop_index("ix_avatar_generation_workspace_id", table_name="avatar_generation")
    op.drop_table("avatar_generation")
    op.drop_index("ix_media_asset_workspace_owner", table_name="media_asset")
    op.drop_index("ix_media_asset_workspace_id", table_name="media_asset")
    op.drop_table("media_asset")
