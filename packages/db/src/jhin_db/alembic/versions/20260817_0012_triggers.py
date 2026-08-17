"""Triggers and trigger invocations (plan 6.11, 9.4, 10).

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trigger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(200), nullable=True),
        sa.Column("filter_json", JSONB(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("target_agent_id", sa.Uuid(), nullable=True),
        sa.Column("target_team_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_definition", JSONB(), nullable=True),
        sa.Column("action_config_json", JSONB(), nullable=False),
        sa.Column("dedupe_window_seconds", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trigger"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_trigger_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection.id"],
            name="fk_trigger_connection_id_connection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_agent_id"],
            ["agent.id"],
            name="fk_trigger_target_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_team_id"],
            ["team.id"],
            name="fk_trigger_target_team_id_team",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_trigger_created_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_trigger_workspace_id", "trigger", ["workspace_id"])
    op.create_index("ix_trigger_connection_id", "trigger", ["connection_id"])
    op.create_index("ix_trigger_event_type", "trigger", ["event_type"])

    op.create_table(
        "trigger_invocation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trigger_invocation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_trigger_invocation_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["trigger.id"],
            name="fk_trigger_invocation_trigger_id_trigger",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            name="fk_trigger_invocation_task_id_task",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_trigger_invocation_workspace_id", "trigger_invocation", ["workspace_id"])
    op.create_index("ix_trigger_invocation_trigger_id", "trigger_invocation", ["trigger_id"])
    op.create_index("ix_trigger_invocation_task_id", "trigger_invocation", ["task_id"])
    op.create_index("ix_trigger_invocation_status", "trigger_invocation", ["status"])
    # Dedupe authority (plan 48.6): at most one *started* invocation per
    # (trigger, idempotency key); duplicate/failed rows remain recordable.
    op.create_index(
        "uq_trigger_invocation_started_key",
        "trigger_invocation",
        ["trigger_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status = 'started'"),
    )


def downgrade() -> None:
    op.drop_table("trigger_invocation")
    op.drop_table("trigger")
