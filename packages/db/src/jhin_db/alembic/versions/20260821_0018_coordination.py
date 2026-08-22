"""Coordination and oversight: ``work_request``, ``review_policy``, and
``work_review``. Purely additive — no existing rows change.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-21
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "work_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("requester_agent_id", sa.Uuid(), nullable=False),
        sa.Column("requester_task_id", sa.Uuid(), nullable=True),
        sa.Column("requester_run_id", sa.Uuid(), nullable=True),
        sa.Column("root_task_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_agent_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("expected_output", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_task_id", sa.Uuid(), nullable=True),
        sa.Column("response", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_work_request"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_work_request_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_work_request_conversation_id_conversation",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requester_agent_id"],
            ["agent.id"],
            name="fk_work_request_requester_agent_id_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requester_task_id"],
            ["task.id"],
            name="fk_work_request_requester_task_id_task",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requester_run_id"],
            ["agent_run.id"],
            name="fk_work_request_requester_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user.id"],
            name="fk_work_request_requested_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_agent_id"],
            ["agent.id"],
            name="fk_work_request_target_agent_id_agent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_task_id"],
            ["task.id"],
            name="fk_work_request_created_task_id_task",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_work_request_workspace_idempotency"
        ),
        sa.UniqueConstraint("created_task_id", name="uq_work_request_created_task"),
        sa.CheckConstraint(
            "requester_agent_id <> target_agent_id", name="ck_work_request_requester_not_target"
        ),
        sa.CheckConstraint("depth >= 1", name="ck_work_request_depth_positive"),
    )
    op.create_index("ix_work_request_workspace_id", "work_request", ["workspace_id"])
    op.create_index("ix_work_request_status", "work_request", ["status"])
    op.create_index(
        "ix_work_request_workspace_target_status",
        "work_request",
        ["workspace_id", "target_agent_id", "status"],
    )
    op.create_index(
        "ix_work_request_workspace_requester",
        "work_request",
        ["workspace_id", "requester_agent_id"],
    )
    op.create_index(
        "ix_work_request_workspace_root_task", "work_request", ["workspace_id", "root_task_id"]
    )

    op.create_table(
        "review_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "scope_kind", sa.String(32), nullable=False, server_default=sa.text("'workspace'")
        ),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("scope_key", sa.String(100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mode", sa.String(32), nullable=False, server_default=sa.text("'before_close'")),
        sa.Column("conditions_json", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "reviewer_selector_json", JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("fail_closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("period_seconds", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_review_policy"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_review_policy_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_review_policy_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "(scope_kind = 'workspace' AND scope_id IS NULL AND scope_key IS NULL) OR "
            "(scope_kind IN ('team', 'agent') AND scope_id IS NOT NULL AND scope_key IS NULL) OR "
            "(scope_kind = 'task_type' AND scope_id IS NULL AND scope_key IS NOT NULL)",
            name="ck_review_policy_scope_shape",
        ),
        sa.CheckConstraint(
            "mode IN ('pre_action', 'before_close', 'post_action', 'periodic')",
            name="ck_review_policy_mode",
        ),
    )
    op.create_index("ix_review_policy_workspace_id", "review_policy", ["workspace_id"])
    op.create_index(
        "ix_review_policy_workspace_enabled", "review_policy", ["workspace_id", "enabled"]
    )

    op.create_table(
        "work_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("work_request_id", sa.Uuid(), nullable=True),
        sa.Column("subject_agent_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_key", sa.String(300), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("evidence_json", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reviewer_type", sa.String(16), nullable=False),
        sa.Column("reviewer_agent_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("verdict", sa.String(32), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_agent_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_work_review"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_work_review_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["review_policy.id"],
            name="fk_work_review_policy_id_review_policy",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_work_review_task_id_task", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_run.id"],
            name="fk_work_review_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_request_id"],
            ["work_request.id"],
            name="fk_work_review_work_request_id_work_request",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_agent_id"],
            ["agent.id"],
            name="fk_work_review_subject_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_agent_id"],
            ["agent.id"],
            name="fk_work_review_reviewer_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["user.id"],
            name="fk_work_review_reviewer_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name="fk_work_review_decided_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_agent_id"],
            ["agent.id"],
            name="fk_work_review_decided_by_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("workspace_id", "trigger_key", name="uq_work_review_workspace_trigger"),
        sa.CheckConstraint(
            "(reviewer_type = 'agent' AND reviewer_agent_id IS NOT NULL "
            "AND reviewer_user_id IS NULL) OR "
            "(reviewer_type = 'human' AND reviewer_agent_id IS NULL) OR "
            "(reviewer_type = 'none' AND reviewer_agent_id IS NULL AND reviewer_user_id IS NULL)",
            name="ck_work_review_reviewer_shape",
        ),
    )
    op.create_index("ix_work_review_workspace_id", "work_review", ["workspace_id"])
    op.create_index("ix_work_review_status", "work_review", ["status"])
    op.create_index("ix_work_review_workspace_status", "work_review", ["workspace_id", "status"])
    op.create_index(
        "ix_work_review_workspace_reviewer_agent",
        "work_review",
        ["workspace_id", "reviewer_agent_id"],
    )
    op.create_index("ix_work_review_workspace_task", "work_review", ["workspace_id", "task_id"])


def downgrade() -> None:
    op.drop_index("ix_work_review_workspace_task", table_name="work_review")
    op.drop_index("ix_work_review_workspace_reviewer_agent", table_name="work_review")
    op.drop_index("ix_work_review_workspace_status", table_name="work_review")
    op.drop_index("ix_work_review_status", table_name="work_review")
    op.drop_index("ix_work_review_workspace_id", table_name="work_review")
    op.drop_table("work_review")
    op.drop_index("ix_review_policy_workspace_enabled", table_name="review_policy")
    op.drop_index("ix_review_policy_workspace_id", table_name="review_policy")
    op.drop_table("review_policy")
    op.drop_index("ix_work_request_workspace_root_task", table_name="work_request")
    op.drop_index("ix_work_request_workspace_requester", table_name="work_request")
    op.drop_index("ix_work_request_workspace_target_status", table_name="work_request")
    op.drop_index("ix_work_request_status", table_name="work_request")
    op.drop_index("ix_work_request_workspace_id", table_name="work_request")
    op.drop_table("work_request")
