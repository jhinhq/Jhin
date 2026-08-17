"""Connections (authenticated integrations) and webhook delivery dedupe
(plan 6.9, 9.4, 19).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connector_type", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("auth_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("encrypted_secret_id", sa.Uuid(), nullable=True),
        sa.Column("webhook_secret_id", sa.Uuid(), nullable=True),
        sa.Column("config_json", JSONB(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_connection_workspace_id_name"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_connection_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["encrypted_secret_id"],
            ["secret.id"],
            name="fk_connection_encrypted_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_secret_id"],
            ["secret.id"],
            name="fk_connection_webhook_secret_id_secret",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_connection_created_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_connection_workspace_id", "connection", ["workspace_id"])
    op.create_index("ix_connection_connector_type", "connection", ["connector_type"])
    op.create_index("ix_connection_public_id", "connection", ["public_id"], unique=True)

    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.String(200), nullable=False),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_delivery"),
        sa.UniqueConstraint(
            "connection_id", "delivery_id", name="uq_webhook_delivery_connection_id_delivery_id"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_webhook_delivery_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection.id"],
            name="fk_webhook_delivery_connection_id_connection",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_webhook_delivery_workspace_id", "webhook_delivery", ["workspace_id"])
    op.create_index("ix_webhook_delivery_connection_id", "webhook_delivery", ["connection_id"])

    # tool_call.connection_id exists since 0009; give connector-usage queries
    # (connection detail "recent tool calls") an index now that it is used.
    op.create_index("ix_tool_call_connection_id", "tool_call", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_connection_id", "tool_call")
    op.drop_table("webhook_delivery")
    op.drop_table("connection")
