"""First-class conversations: the ``conversation`` table plus additive
``conversation_id`` links on ``task`` and ``message``.

Backfill: every legacy "Message an agent" task (``metadata_json.origin ==
"message"``) becomes one conversation so existing chats keep their history.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("primary_agent_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_conversation_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["primary_agent_id"],
            ["agent.id"],
            name="fk_conversation_primary_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_conversation_created_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_conversation_workspace_id", "conversation", ["workspace_id"])
    op.create_index(
        "ix_conversation_workspace_last_activity",
        "conversation",
        ["workspace_id", sa.text("last_activity_at DESC")],
    )
    op.create_index(
        "ix_conversation_workspace_primary_agent",
        "conversation",
        ["workspace_id", "primary_agent_id"],
    )

    op.add_column("task", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_task_conversation_id_conversation",
        "task",
        "conversation",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_task_conversation_id", "task", ["conversation_id"])

    op.add_column("message", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_message_conversation_id_conversation",
        "message",
        "conversation",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])

    _backfill_legacy_message_tasks()


def _backfill_legacy_message_tasks() -> None:
    task = sa.table(
        "task",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("title", sa.String()),
        sa.column("assigned_agent_id", sa.Uuid()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("metadata_json", JSONB()),
        sa.column("conversation_id", sa.Uuid()),
    )
    message = sa.table(
        "message",
        sa.column("task_id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
    )
    conversation = sa.table(
        "conversation",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("title", sa.String()),
        sa.column("status", sa.String()),
        sa.column("pinned", sa.Boolean()),
        sa.column("primary_agent_id", sa.Uuid()),
        sa.column("created_by_user_id", sa.Uuid()),
        sa.column("last_activity_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    legacy_tasks = bind.execute(
        sa.select(
            task.c.id,
            task.c.workspace_id,
            task.c.title,
            task.c.assigned_agent_id,
            task.c.updated_at,
        ).where(task.c.metadata_json["origin"].astext == "message")
    ).mappings()
    for row in legacy_tasks:
        conversation_id = new_uuid7()
        bind.execute(
            conversation.insert().values(
                id=conversation_id,
                workspace_id=row["workspace_id"],
                title=(row["title"] or "Conversation")[:200],
                status="active",
                pinned=False,
                primary_agent_id=row["assigned_agent_id"],
                created_by_user_id=None,
                last_activity_at=row["updated_at"],
                created_at=row["updated_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            task.update().where(task.c.id == row["id"]).values(conversation_id=conversation_id)
        )
        bind.execute(
            message.update()
            .where(message.c.task_id == row["id"])
            .values(conversation_id=conversation_id)
        )


def downgrade() -> None:
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_constraint("fk_message_conversation_id_conversation", "message", type_="foreignkey")
    op.drop_column("message", "conversation_id")
    op.drop_index("ix_task_conversation_id", table_name="task")
    op.drop_constraint("fk_task_conversation_id_conversation", "task", type_="foreignkey")
    op.drop_column("task", "conversation_id")
    op.drop_index("ix_conversation_workspace_primary_agent", table_name="conversation")
    op.drop_index("ix_conversation_workspace_last_activity", table_name="conversation")
    op.drop_index("ix_conversation_workspace_id", table_name="conversation")
    op.drop_table("conversation")
