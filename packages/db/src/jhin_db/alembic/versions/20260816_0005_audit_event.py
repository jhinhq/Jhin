"""Append-only audit trail (plan 6.17, 23).

No application code path may UPDATE or DELETE rows in this table.
workspace_id intentionally has no foreign key so history survives
workspace deletion.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("target_type", sa.String(60), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("ip_hash", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
    )
    op.create_index("ix_audit_event_action", "audit_event", ["action"])
    op.create_index(
        "ix_audit_event_workspace_created", "audit_event", ["workspace_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("audit_event")
