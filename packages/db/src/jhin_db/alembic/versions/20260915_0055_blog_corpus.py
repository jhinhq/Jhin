"""Versioned full Ghost archive coverage and local article bodies.

Revision ID: 0055
Revises: 0054
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _ref(name: str, table: str) -> sa.Column[Any]:
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "blog_corpus_sync",
        *_base(),
        _ref("workspace_id", "workspace"),
        _ref("connection_id", "connection"),
        _ref("assignment_id", "editorial_assignment"),
        _ref("agent_id", "agent"),
        _ref("task_id", "task"),
        _ref("run_id", "agent_run"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("active_key", sa.String(100)),
        sa.Column("request_key", sa.String(100)),
        sa.Column("next_page", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("pass_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expected_total", sa.Integer()),
        sa.Column("discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("previous_manifest", sa.String(64)),
        sa.Column("corpus_hash", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("index_version", sa.String(50), nullable=False, server_default="lexical-v1"),
        sa.UniqueConstraint("workspace_id", "active_key", name="uq_corpus_active_sync"),
        sa.UniqueConstraint("workspace_id", "request_key", name="uq_corpus_request"),
    )
    op.create_index("ix_blog_corpus_sync_status", "blog_corpus_sync", ["status"])
    op.create_table(
        "blog_corpus_document",
        *_base(),
        _ref("workspace_id", "workspace"),
        _ref("sync_id", "blog_corpus_sync"),
        sa.Column("post_id", sa.String(24), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("provider_revision", sa.String(64), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("seen_pass", sa.Integer(), nullable=False),
        sa.UniqueConstraint("sync_id", "post_id", name="uq_corpus_document_version"),
    )
    op.create_index("ix_blog_corpus_document_sync_id", "blog_corpus_document", ["sync_id"])


def downgrade() -> None:
    op.drop_table("blog_corpus_document")
    op.drop_table("blog_corpus_sync")
