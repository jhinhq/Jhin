"""Capability grants, approvals, tool calls, and per-agent approval policy
(plan 6.6, 6.15, 6.16, 42).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_capability_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(200), nullable=False),
        sa.Column("scope_json", JSONB(), nullable=False),
        sa.Column("effect", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_capability_grant"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_agent_capability_grant_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            name="fk_agent_capability_grant_agent_id_agent",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_agent_capability_grant_workspace_id", "agent_capability_grant", ["workspace_id"]
    )
    op.create_index("ix_agent_capability_grant_agent_id", "agent_capability_grant", ["agent_id"])

    op.create_table(
        "approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_agent_id", sa.Uuid(), nullable=True),
        sa.Column("action_type", sa.String(200), nullable=False),
        sa.Column("action_payload_sanitized", JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_approval_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_approval_task_id_task", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_run.id"], name="fk_approval_run_id_agent_run", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_agent_id"],
            ["agent.id"],
            name="fk_approval_requested_by_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name="fk_approval_decided_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_approval_workspace_id", "approval", ["workspace_id"])
    op.create_index("ix_approval_task_id", "approval", ["task_id"])
    op.create_index("ix_approval_run_id", "approval", ["run_id"])
    op.create_index("ix_approval_requested_by_agent_id", "approval", ["requested_by_agent_id"])
    op.create_index("ix_approval_status", "approval", ["status"])

    op.create_table(
        "tool_call",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("sanitized_input_json", JSONB(), nullable=False),
        sa.Column("sanitized_output_json", JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_call"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_tool_call_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_run.id"], name="fk_tool_call_run_id_agent_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent.id"], name="fk_tool_call_agent_id_agent", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approval.id"],
            name="fk_tool_call_approval_id_approval",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_tool_call_workspace_id", "tool_call", ["workspace_id"])
    op.create_index("ix_tool_call_run_id", "tool_call", ["run_id"])
    op.create_index("ix_tool_call_agent_id", "tool_call", ["agent_id"])
    op.create_index("ix_tool_call_status", "tool_call", ["status"])

    op.add_column(
        "agent",
        sa.Column(
            "approval_policy_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent", "approval_policy_json")
    op.drop_table("tool_call")
    op.drop_table("approval")
    op.drop_table("agent_capability_grant")
