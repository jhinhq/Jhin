"""Required questions and durable local-time schedules.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "user_question",
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_question", sa.Column("input_key", sa.String(100), nullable=False, server_default="")
    )
    op.add_column(
        "user_question",
        sa.Column("value_type", sa.String(16), nullable=False, server_default="text"),
    )
    op.create_table(
        "agent_schedule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("local_time", sa.String(5), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("weekdays", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("overlap_policy", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(32), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("created_by_agent_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="SET NULL")),
        *timestamps(),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_agent_schedule_workspace_id_idempotency_key"
        ),
    )
    for key in ("workspace_id", "agent_id", "next_run_at"):
        op.create_index(f"ix_agent_schedule_{key}", "agent_schedule", [key])
    op.create_table(
        "schedule_occurrence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "schedule_id",
            sa.Uuid(),
            sa.ForeignKey("agent_schedule.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("task.id", ondelete="SET NULL")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_schedule_occurrence_schedule_id_scheduled_for"
        ),
    )
    op.create_index(
        "ix_schedule_occurrence_history",
        "schedule_occurrence",
        ["workspace_id", "schedule_id", "scheduled_for"],
    )


def downgrade() -> None:
    # Preserve disabled schedules, completed history, and resolved answers too:
    # each carries durable state that an older schema cannot represent.
    connection = op.get_bind()
    for table in ("schedule_occurrence", "agent_schedule"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "Cannot downgrade while retained schedules or occurrence history exist"
            )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM user_question "
            "WHERE required = TRUE OR input_key <> '' OR value_type <> 'text' LIMIT 1"
        )
    ).first():
        raise RuntimeError("Cannot downgrade while required or structured question records exist")
    op.drop_table("schedule_occurrence")
    op.drop_table("agent_schedule")
    op.drop_column("user_question", "value_type")
    op.drop_column("user_question", "input_key")
    op.drop_column("user_question", "required")
