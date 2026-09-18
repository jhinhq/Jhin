"""Persistent chat workspaces, projects, immutable files and review records.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id(
    name: str, target: str | None = None, *, nullable: bool = False, ondelete: str = "CASCADE"
) -> sa.Column[Any]:
    args = [sa.ForeignKey(target, ondelete=ondelete)] if target else []
    return sa.Column(name, sa.Uuid(), *args, nullable=nullable, primary_key=name == "id")


def _common(*, mutable: bool = False) -> list[sa.Column[Any]]:
    cols = [
        _id("id"),
        _id("workspace_id", "workspace.id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]
    if mutable:
        cols.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
    return cols


def _json(name: str, *, array: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE secret SET type = 'composio_binding' WHERE id IN "
            "(SELECT encrypted_secret_id FROM connection "
            "WHERE oauth_issuer = 'https://composio.dev') "
            "AND type = 'connection_credentials'"
        )
    )
    op.create_table(
        "chat_project",
        *_common(mutable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("repository_url", sa.String(2000)),
        sa.Column("source_revision", sa.String(300)),
        sa.Column(
            "source_manifest_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        _id("created_by_user_id", "user.id", nullable=True, ondelete="SET NULL"),
    )
    op.create_index("ix_chat_project_workspace_id", "chat_project", ["workspace_id"])
    op.add_column("conversation", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.add_column(
        "conversation",
        sa.Column("workspace_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_conversation_project_id_chat_project",
        "conversation",
        "chat_project",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_conversation_project_id", "conversation", ["project_id"])
    op.alter_column("sandbox_workspace", "agent_id", nullable=True)
    op.add_column("sandbox_workspace", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.add_column("sandbox_workspace", sa.Column("holder_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "sandbox_workspace",
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_sandbox_workspace_conversation_id_conversation",
        "sandbox_workspace",
        "conversation",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_sandbox_workspace_holder_user_id_user",
        "sandbox_workspace",
        "user",
        ["holder_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_sandbox_workspace_conversation_id", "sandbox_workspace", ["conversation_id"]
    )
    op.create_index(
        "uq_sandbox_workspace_conversation",
        "sandbox_workspace",
        ["workspace_id", "conversation_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'conversation'"),
        sqlite_where=sa.text("kind = 'conversation'"),
    )
    op.create_table(
        "managed_file",
        *_common(mutable=True),
        _id("conversation_id", "conversation.id"),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("current_revision_id", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False),
        _id("created_by_user_id", "user.id", nullable=True, ondelete="SET NULL"),
        sa.UniqueConstraint(
            "workspace_id", "conversation_id", "path", name="uq_managed_file_conversation_path"
        ),
    )
    op.create_table(
        "file_revision",
        *_common(),
        _id("file_id", "managed_file.id"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(200), nullable=False),
        sa.Column("preview_kind", sa.String(20), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("extraction_truncated", sa.Boolean(), nullable=False),
        _json("metadata_json"),
        _id("created_by_user_id", "user.id", nullable=True, ondelete="SET NULL"),
        _id("source_run_id", "agent_run.id", nullable=True, ondelete="SET NULL"),
        sa.UniqueConstraint("file_id", "version", name="uq_file_revision_version"),
    )
    op.create_table(
        "file_checkpoint",
        *_common(),
        _id("conversation_id", "conversation.id"),
        sa.Column("label", sa.String(200), nullable=False),
        _json("manifest_json"),
        _json("excluded_json", array=True),
        _id("created_by_user_id", "user.id", nullable=True, ondelete="SET NULL"),
        _id("source_run_id", "agent_run.id", nullable=True, ondelete="SET NULL"),
    )
    op.create_table(
        "file_annotation",
        *_common(),
        _id("file_id", "managed_file.id"),
        _id("revision_id", "file_revision.id"),
        sa.Column("text", sa.Text(), nullable=False),
        _json("location_json"),
        _id("created_by_user_id", "user.id", nullable=True, ondelete="SET NULL"),
    )
    for table, association in [
        ("managed_file", "conversation_id"),
        ("file_revision", "file_id"),
        ("file_checkpoint", "conversation_id"),
        ("file_annotation", "file_id"),
    ]:
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
        op.create_index(f"ix_{table}_{association}", table, [association])


def downgrade() -> None:
    # A destructive downgrade is deliberately refused when retained chats use
    # the new schema. Back up/export first; migrations never silently drop files.
    bind = op.get_bind()
    if (
        bind.scalar(sa.text("SELECT count(*) FROM chat_project"))
        or bind.scalar(sa.text("SELECT count(*) FROM managed_file"))
        or bind.scalar(
            sa.text("SELECT count(*) FROM sandbox_workspace WHERE kind = 'conversation'")
        )
    ):
        raise RuntimeError(
            "Cannot downgrade retained chat files/workspaces; "
            "export and remove them explicitly first"
        )
    op.execute(
        sa.text("UPDATE secret SET type = 'connection_credentials' WHERE type = 'composio_binding'")
    )
    for table in ["file_annotation", "file_checkpoint", "file_revision", "managed_file"]:
        op.drop_table(table)
    op.drop_index("uq_sandbox_workspace_conversation", table_name="sandbox_workspace")
    op.drop_index("ix_sandbox_workspace_conversation_id", table_name="sandbox_workspace")
    op.drop_constraint(
        "fk_sandbox_workspace_conversation_id_conversation", "sandbox_workspace", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_sandbox_workspace_holder_user_id_user", "sandbox_workspace", type_="foreignkey"
    )
    for column in ["conversation_id", "holder_user_id", "lease_generation"]:
        op.drop_column("sandbox_workspace", column)
    op.alter_column("sandbox_workspace", "agent_id", nullable=False)
    op.drop_index("ix_conversation_project_id", table_name="conversation")
    op.drop_constraint(
        "fk_conversation_project_id_chat_project", "conversation", type_="foreignkey"
    )
    op.drop_column("conversation", "project_id")
    op.drop_column("conversation", "workspace_version")
    op.drop_table("chat_project")
