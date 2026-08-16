"""Tasks, agent runs, messages, and the persisted run timeline (plan 6.12-6.14).

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("external_source", sa.String(64), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("assigned_agent_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_team_id", sa.Uuid(), nullable=True),
        sa.Column("parent_task_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_id", sa.Uuid(), nullable=True),
        sa.Column("temporal_workflow_id", sa.String(200), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_task_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"],
            ["agent.id"],
            name="fk_task_assigned_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_team_id"],
            ["team.id"],
            name="fk_task_assigned_team_id_team",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"], ["task.id"], name="fk_task_parent_task_id_task", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_task_workspace_id", "task", ["workspace_id"])
    op.create_index("ix_task_state", "task", ["state"])
    op.create_index("ix_task_assigned_agent_id", "task", ["assigned_agent_id"])

    op.create_table(
        "agent_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("model_profile_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("steps_used", sa.Integer(), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(200), nullable=True),
        sa.Column("temporal_run_id", sa.String(200), nullable=True),
        sa.Column("langgraph_thread_id", sa.String(200), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_run_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent.id"], name="fk_agent_run_agent_id_agent", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_agent_run_task_id_task", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["agent_run.id"],
            name="fk_agent_run_parent_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_profile_id"],
            ["model_profile.id"],
            name="fk_agent_run_model_profile_id_model_profile",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_agent_run_workspace_id", "agent_run", ["workspace_id"])
    op.create_index("ix_agent_run_agent_id", "agent_run", ["agent_id"])
    op.create_index("ix_agent_run_task_id", "agent_run", ["task_id"])
    op.create_index("ix_agent_run_status", "agent_run", ["status"])

    op.create_table(
        "message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("sender_type", sa.String(16), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_type", sa.String(16), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content_json", JSONB(), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_message_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_message_task_id_task", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_run.id"], name="fk_message_run_id_agent_run", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_message_workspace_id", "message", ["workspace_id"])
    op.create_index("ix_message_task_id", "message", ["task_id"])
    op.create_index("ix_message_run_id", "message", ["run_id"])

    op.create_table(
        "run_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_event"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_run_event_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_run.id"], name="fk_run_event_run_id_agent_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_run_event_task_id_task", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_run_event_workspace_id", "run_event", ["workspace_id"])
    op.create_index("ix_run_event_run_id", "run_event", ["run_id"])
    op.create_index("ix_run_event_task_id", "run_event", ["task_id"])


def downgrade() -> None:
    op.drop_table("run_event")
    op.drop_table("message")
    op.drop_table("agent_run")
    op.drop_table("task")
