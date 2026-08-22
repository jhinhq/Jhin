"""Curated long-term memory: the versioned ``memory_record`` table.

On PostgreSQL this migration tries ``CREATE EXTENSION IF NOT EXISTS vector``
best-effort. When the extension is available an additional raw
``embedding_vec vector`` column is created for pgvector operators; when it is
not (missing package, insufficient privilege) the base schema is still valid
and retrieval degrades to PostgreSQL full-text search over ``content``
(a ``to_tsvector`` GIN index is created either way). SQLite keeps the
portable JSON embedding only.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_list() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _try_enable_pgvector() -> bool:
    """Best-effort ``CREATE EXTENSION``; returns whether ``vector`` exists."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    try:
        with bind.begin_nested():
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:
        pass
    try:
        row = bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
    except Exception:
        return False
    return row is not None


def upgrade() -> None:
    op.create_table(
        "memory_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("source_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("source_task_id", sa.Uuid(), nullable=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("sensitivity", sa.String(16), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("importance", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("tags_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("embedding_json", _json_list(), nullable=True),
        sa.Column("embedding_model", sa.String(200), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("policy_json", _json_list(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_record"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_memory_record_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["conversation.id"],
            name="fk_memory_record_source_conversation_id_conversation",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["message.id"],
            name="fk_memory_record_source_message_id_message",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id"],
            ["task.id"],
            name="fk_memory_record_source_task_id_task",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["memory_record.id"],
            name="fk_memory_record_supersedes_id_memory_record",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_memory_record_workspace_id", "memory_record", ["workspace_id"])
    op.create_index(
        "ix_memory_record_workspace_scope",
        "memory_record",
        ["workspace_id", "scope", "scope_id", "status"],
    )
    op.create_index(
        "ix_memory_record_workspace_hash", "memory_record", ["workspace_id", "content_hash"]
    )
    op.create_index(
        "ix_memory_record_workspace_subject", "memory_record", ["workspace_id", "subject"]
    )
    op.create_index("ix_memory_record_expires_at", "memory_record", ["expires_at"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Full-text fallback is always available on PostgreSQL.
        op.execute(
            "CREATE INDEX ix_memory_record_content_fts ON memory_record "
            "USING GIN (to_tsvector('english', content))"
        )
        if _try_enable_pgvector():
            op.execute("ALTER TABLE memory_record ADD COLUMN embedding_vec vector")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memory_record_content_fts")
    op.drop_index("ix_memory_record_expires_at", table_name="memory_record")
    op.drop_index("ix_memory_record_workspace_subject", table_name="memory_record")
    op.drop_index("ix_memory_record_workspace_hash", table_name="memory_record")
    op.drop_index("ix_memory_record_workspace_scope", table_name="memory_record")
    op.drop_index("ix_memory_record_workspace_id", table_name="memory_record")
    op.drop_table("memory_record")
