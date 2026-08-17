"""Sandbox job records (plan 14): one row per ephemeral job container.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("image", sa.String(300), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("network_policy", sa.String(16), nullable=False),
        sa.Column("cpu_limit", sa.Float(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("pids_limit", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=False),
        sa.Column("stderr_tail", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_job"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_sandbox_job_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_run.id"],
            name="fk_sandbox_job_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            name="fk_sandbox_job_task_id_task",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_call.id"],
            name="fk_sandbox_job_tool_call_id_tool_call",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_sandbox_job_workspace_id", "sandbox_job", ["workspace_id"])
    op.create_index("ix_sandbox_job_run_id", "sandbox_job", ["run_id"])
    op.create_index("ix_sandbox_job_task_id", "sandbox_job", ["task_id"])
    op.create_index("ix_sandbox_job_tool_call_id", "sandbox_job", ["tool_call_id"])
    op.create_index("ix_sandbox_job_status", "sandbox_job", ["status"])


def downgrade() -> None:
    op.drop_table("sandbox_job")
