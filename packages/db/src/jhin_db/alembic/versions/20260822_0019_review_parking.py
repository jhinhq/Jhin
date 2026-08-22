"""Durable review parking: ``tool_call.review_id`` binds a call persisted as
``pending_review`` to the pre-action ``work_review`` it waits on. Purely
additive — no existing rows change.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_call", sa.Column("review_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_tool_call_review_id_work_review",
        "tool_call",
        "work_review",
        ["review_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tool_call_review_id", "tool_call", ["review_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_review_id", table_name="tool_call")
    op.drop_constraint("fk_tool_call_review_id_work_review", "tool_call", type_="foreignkey")
    op.drop_column("tool_call", "review_id")
