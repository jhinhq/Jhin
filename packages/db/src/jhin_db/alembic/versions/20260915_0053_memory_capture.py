"""Prospective memory capture authority.

Revision ID: 0053
Revises: 0052
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_capture_policy",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column(
            "granted_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("allowed_classes_json", sa.JSON(), nullable=False),
        sa.Column("actor_ids_json", sa.JSON(), nullable=False),
        sa.Column("allowed_source_agent_ids_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "source_user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_after_message_id", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_memory_capture_destination",
        "memory_capture_policy",
        ["workspace_id", "scope", "scope_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_capture_destination", table_name="memory_capture_policy")
    op.drop_table("memory_capture_policy")
