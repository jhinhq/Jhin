"""Questions an agent puts to a person, and the authority their answer carries.

Purely additive: one new table. The ``granted_*`` columns are the reason it
is a table rather than a message field — a memory wider than the agent's own
is authorised by what the API wrote here from the answering user's RBAC, and
never by anything a model says.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "user_question",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default=sa.text("'open'")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("options_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("allow_other", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("dedupe_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("answer_kind", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "answer_option_value", sa.String(64), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("answer_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("granted_scope", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column("granted_authority", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "grant_denied_reason", sa.String(64), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("grant_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_consumed_tool_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_question"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_user_question_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_user_question_conversation_id_conversation",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task.id"], name="fk_user_question_task_id_task", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_run.id"],
            name="fk_user_question_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent.id"], name="fk_user_question_agent_id_agent", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name="fk_user_question_message_id_message",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by_user_id"],
            ["user.id"],
            name="fk_user_question_answered_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_user_question_workspace_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'expired', 'cancelled')",
            name="ck_user_question_status",
        ),
        sa.CheckConstraint(
            "granted_scope IN ('', 'agent', 'team', 'workspace')",
            name="ck_user_question_granted_scope",
        ),
        sa.CheckConstraint(
            "answer_kind IN ('', 'option', 'other')", name="ck_user_question_answer_kind"
        ),
    )
    op.create_index("ix_user_question_workspace_id", "user_question", ["workspace_id"])
    op.create_index(
        "ix_user_question_workspace_status", "user_question", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_user_question_workspace_conversation",
        "user_question",
        ["workspace_id", "conversation_id", "status"],
    )
    op.create_index(
        "ix_user_question_workspace_agent_dedupe",
        "user_question",
        ["workspace_id", "agent_id", "dedupe_hash"],
    )
    op.create_index("ix_user_question_workspace_run", "user_question", ["workspace_id", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_user_question_workspace_run", table_name="user_question")
    op.drop_index("ix_user_question_workspace_agent_dedupe", table_name="user_question")
    op.drop_index("ix_user_question_workspace_conversation", table_name="user_question")
    op.drop_index("ix_user_question_workspace_status", table_name="user_question")
    op.drop_index("ix_user_question_workspace_id", table_name="user_question")
    op.drop_table("user_question")
