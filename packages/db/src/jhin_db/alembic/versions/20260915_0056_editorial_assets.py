"""Persist user-selected image attribution and tracking state."""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_asset",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("editorial_assignment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("connection.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.Uuid(),
            sa.ForeignKey("user_question.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("photo_id", sa.String(100), nullable=False),
        sa.Column(
            "selected_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tracking_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("tracking_receipt_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("assignment_id", "question_id"),
        sa.UniqueConstraint("workspace_id", "question_id", name="uq_asset_human_choice"),
    )


def downgrade() -> None:
    op.drop_table("editorial_asset")
