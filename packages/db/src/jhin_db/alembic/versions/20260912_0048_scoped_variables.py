"""Scoped variables and encrypted secure-input receipts.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "scoped_variable",
        *_common(),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        sa.Column("plaintext", sa.Text()),
        sa.Column(
            "secret_id", sa.Uuid(), sa.ForeignKey("secret.id", ondelete="RESTRICT"), unique=True
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_variable_id", sa.Uuid()),
        sa.Column("source_version", sa.Integer()),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_type", sa.String(16), nullable=False),
        sa.Column("updated_by_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "scope", "scope_id", "name", name="uq_variable_namespace"
        ),
        sa.CheckConstraint("scope IN ('agent', 'team', 'company')", name="ck_variable_scope"),
        sa.CheckConstraint(
            "(sensitive AND plaintext IS NULL AND secret_id IS NOT NULL) OR "
            "(NOT sensitive AND plaintext IS NOT NULL AND secret_id IS NULL)",
            name="ck_variable_storage",
        ),
    )
    op.create_table(
        "secure_input_capture",
        *_common(),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "secret_id",
            sa.Uuid(),
            sa.ForeignKey("secret.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "variable_id", sa.Uuid(), sa.ForeignKey("scoped_variable.id", ondelete="SET NULL")
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "user_id",
            "fingerprint",
            name="uq_capture_chat_value",
        ),
    )
    op.create_table(
        "variable_connection_binding",
        *_common(),
        sa.Column(
            "variable_id",
            sa.Uuid(),
            sa.ForeignKey("scoped_variable.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_field", sa.String(120), nullable=False),
        sa.Column("approved_origin", sa.String(2048), nullable=False),
        sa.Column(
            "created_by_agent_id",
            sa.Uuid(),
            sa.ForeignKey("agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workspace_id", "connection_id", "credential_field", name="uq_variable_connection_field"
        ),
    )
    for table, columns in {
        "scoped_variable": ("workspace_id", "scope_id"),
        "secure_input_capture": ("workspace_id", "conversation_id", "agent_id"),
        "variable_connection_binding": ("workspace_id", "variable_id", "connection_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    # Application rollback can leave additive state intact. Refuse to silently
    # discard a configured variable or a still-private credential capture.
    connection = op.get_bind()
    for table in ("scoped_variable", "secure_input_capture"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot downgrade while retained scoped variables or secure inputs exist"
            )
    op.drop_table("variable_connection_binding")
    op.drop_table("secure_input_capture")
    op.drop_table("scoped_variable")
