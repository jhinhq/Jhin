"""Access control: workspace invitations, scoped API keys, and API-key usage
(docs/architecture/rbac.md, docs/architecture/api-keys.md).

Purely additive.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "workspace_invitation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default=sa.text("'member'")),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_invitation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_workspace_invitation_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["user.id"],
            name="fk_workspace_invitation_invited_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_user_id"],
            ["user.id"],
            name="fk_workspace_invitation_accepted_user_id_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("token_hash", name="uq_workspace_invitation_token_hash"),
    )
    op.create_index(
        "ix_workspace_invitation_workspace_id", "workspace_invitation", ["workspace_id"]
    )
    op.create_index(
        "ix_workspace_invitation_workspace_email",
        "workspace_invitation",
        ["workspace_id", "email"],
    )

    op.create_table(
        "api_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "role_ceiling", sa.String(32), nullable=False, server_default=sa.text("'viewer'")
        ),
        sa.Column("scopes_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_key"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_api_key_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_api_key_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("prefix", name="uq_api_key_prefix"),
    )
    op.create_index("ix_api_key_workspace_id", "api_key", ["workspace_id"])
    op.create_index("ix_api_key_prefix", "api_key", ["prefix"])

    op.create_table(
        "api_key_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("acting_user_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("ip_hash", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_key_usage"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_api_key_usage_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_key.id"],
            name="fk_api_key_usage_api_key_id_api_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acting_user_id"],
            ["user.id"],
            name="fk_api_key_usage_acting_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_api_key_usage_workspace_id", "api_key_usage", ["workspace_id"])
    op.create_index("ix_api_key_usage_api_key_id", "api_key_usage", ["api_key_id"])
    op.create_index(
        "ix_api_key_usage_workspace_created", "api_key_usage", ["workspace_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_api_key_usage_workspace_created", table_name="api_key_usage")
    op.drop_index("ix_api_key_usage_api_key_id", table_name="api_key_usage")
    op.drop_index("ix_api_key_usage_workspace_id", table_name="api_key_usage")
    op.drop_table("api_key_usage")
    op.drop_index("ix_api_key_prefix", table_name="api_key")
    op.drop_index("ix_api_key_workspace_id", table_name="api_key")
    op.drop_table("api_key")
    op.drop_index("ix_workspace_invitation_workspace_email", table_name="workspace_invitation")
    op.drop_index("ix_workspace_invitation_workspace_id", table_name="workspace_invitation")
    op.drop_table("workspace_invitation")
