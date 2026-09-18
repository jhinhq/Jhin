"""Scoped interactive runtime sessions.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("workspace_key", sa.String(96), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("network", sa.String(16), nullable=False),
        sa.Column("ticket_hash", sa.String(64), nullable=False),
        sa.Column("ticket_expires_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("config_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("state_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for name in (
        "workspace_id",
        "conversation_id",
        "user_id",
        "status",
        "ticket_hash",
        "expires_at",
    ):
        op.create_index(f"ix_runtime_session_{name}", "runtime_session", [name])
    # Only public sessions enter the journal; capability/operation rows do not.
    for event in ("INSERT", "UPDATE", "DELETE"):
        source = "OLD" if event == "DELETE" else "NEW"
        op.execute(
            f"CREATE TRIGGER journal_runtime_session_{event.lower()} AFTER {event} "
            "ON runtime_session FOR EACH ROW "
            f"WHEN ({source}.kind IN ('terminal','preview')) "
            "EXECUTE FUNCTION jhin_conversation_journal()"
        )


def downgrade() -> None:
    for event in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER journal_runtime_session_{event} ON runtime_session")
    op.drop_table("runtime_session")
