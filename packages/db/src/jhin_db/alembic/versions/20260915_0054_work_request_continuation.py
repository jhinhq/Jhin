"""Durable work-request result continuation outbox.

Revision ID: 0054
Revises: 0053
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_request", sa.Column("continuation_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column("work_request", sa.Column("continuation_task_id", sa.Uuid()))
    op.add_column(
        "work_request", sa.Column("continuation_dispatched_at", sa.DateTime(timezone=True))
    )
    op.add_column("work_request", sa.Column("continuation_suppressed_reason", sa.String(100)))
    op.create_foreign_key(
        "fk_work_request_continuation_task_id_task",
        "work_request",
        "task",
        ["continuation_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_work_request_continuation_task", "work_request", ["continuation_task_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_work_request_continuation_task", "work_request", type_="unique")
    op.drop_constraint(
        "fk_work_request_continuation_task_id_task", "work_request", type_="foreignkey"
    )
    for name in (
        "continuation_suppressed_reason",
        "continuation_dispatched_at",
        "continuation_task_id",
        "continuation_requested_at",
    ):
        op.drop_column("work_request", name)
