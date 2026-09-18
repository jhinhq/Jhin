"""Bind native Ghost reviews to assignments, exact packages, and release intent.

Revision ID: 0052
Revises: 0051
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0052"
down_revision = "0051"
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


def _ref(
    name: str, table: str, *, nullable: bool = False, ondelete: str = "RESTRICT"
) -> sa.Column[Any]:
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(f"{table}.id", ondelete=ondelete), nullable=nullable
    )


def _json(name: str, default: str = "{}") -> sa.Column[Any]:
    return sa.Column(
        name, sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False, server_default=default
    )


def upgrade() -> None:
    op.create_table(
        "ghost_installation",
        *_base(),
        _ref("workspace_id", "workspace", ondelete="CASCADE"),
        sa.Column("admin_url", sa.String(2000), nullable=False),
        _ref("publisher_agent_id", "agent"),
        sa.UniqueConstraint("workspace_id", "admin_url"),
    )
    op.create_table(
        "editorial_assignment",
        *_base(),
        _ref("workspace_id", "workspace", ondelete="CASCADE"),
        _ref("connection_id", "connection"),
        _ref("writer_agent_id", "agent"),
        _ref("publisher_agent_id", "agent"),
        _ref("task_id", "task", nullable=True, ondelete="SET NULL"),
        _ref("conversation_id", "conversation", nullable=True, ondelete="SET NULL"),
        _ref("team_id", "team", nullable=True, ondelete="SET NULL"),
        sa.Column("public_blog_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("post_id", sa.String(24)),
        sa.Column("release_intent", sa.String(40), nullable=False, server_default="draft_only"),
        _json("brief_json"),
        _json("decision_provenance"),
        _json("evidence_tool_call_ids", "[]"),
        sa.Column("brief_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("editorial_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(40), nullable=False, server_default="brief"),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("resume_condition", sa.Text()),
        sa.UniqueConstraint("connection_id", "post_id", name="uq_editorial_owned_post"),
        sa.CheckConstraint(
            "release_intent IN ('draft_only','publish_after_ashley_review')",
            name="editorial_release_intent",
        ),
    )
    op.create_index(
        "ix_editorial_assignment_workspace_id", "editorial_assignment", ["workspace_id"]
    )
    op.create_table(
        "editorial_review_package",
        *_base(),
        _ref("workspace_id", "workspace", ondelete="CASCADE"),
        _ref("assignment_id", "editorial_assignment", ondelete="CASCADE"),
        sa.Column("editorial_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        _json("manifest_json"),
        sa.UniqueConstraint("assignment_id", "revision"),
    )
    op.create_table(
        "ghost_review_read_receipt",
        *_base(),
        _ref("workspace_id", "workspace", ondelete="CASCADE"),
        _ref("connection_id", "connection", ondelete="CASCADE"),
        _ref("agent_id", "agent", ondelete="CASCADE"),
        sa.Column("post_id", sa.String(24), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("total_chars", sa.Integer(), nullable=False),
        _ref("package_id", "editorial_review_package", nullable=True, ondelete="CASCADE"),
    )
    with op.batch_alter_table("ghost_editorial_review") as batch:
        batch.add_column(sa.Column("assignment_id", sa.Uuid()))
        batch.add_column(sa.Column("package_id", sa.Uuid()))
        batch.add_column(sa.Column("assignment_editorial_version", sa.Integer()))
        batch.add_column(
            sa.Column("release_intent", sa.String(40), nullable=False, server_default="draft_only")
        )
        batch.add_column(
            sa.Column("revision_round", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("prior_review_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_ghost_editorial_review_assignment_id_editorial_assignment",
            "editorial_assignment",
            ["assignment_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_ghost_editorial_review_package_id_editorial_review_package",
            "editorial_review_package",
            ["package_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_ghost_review_prior_review",
            "ghost_editorial_review",
            ["prior_review_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.drop_constraint("uq_ghost_review_revision_publisher", type_="unique")
        batch.create_unique_constraint(
            "uq_ghost_review_revision_publisher",
            ["connection_id", "post_id", "revision", "publisher_agent_id", "package_id"],
        )
    # Retain old published/uncertain evidence verbatim. Unbound pending/approved
    # reviews cannot authorize new effects and must be resubmitted under a brief.
    op.execute(
        sa.text(
            "UPDATE ghost_editorial_review SET status='stale' "
            "WHERE assignment_id IS NULL AND status IN ('pending','approved')"
        )
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM editorial_assignment)")):
        raise RuntimeError(
            "Remove editorial assignments only after preserving their audit history; "
            "release guards cannot be downgraded in use"
        )
    with op.batch_alter_table("ghost_editorial_review") as batch:
        batch.drop_constraint("uq_ghost_review_revision_publisher", type_="unique")
        batch.create_unique_constraint(
            "uq_ghost_review_revision_publisher",
            ["connection_id", "post_id", "revision", "publisher_agent_id"],
        )
        batch.drop_constraint(
            "fk_ghost_editorial_review_assignment_id_editorial_assignment", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_ghost_editorial_review_package_id_editorial_review_package", type_="foreignkey"
        )
        batch.drop_constraint("fk_ghost_review_prior_review", type_="foreignkey")
        for name in (
            "assignment_id",
            "package_id",
            "assignment_editorial_version",
            "release_intent",
            "revision_round",
            "prior_review_id",
        ):
            batch.drop_column(name)
    for name in (
        "ghost_review_read_receipt",
        "editorial_review_package",
        "editorial_assignment",
        "ghost_installation",
    ):
        op.drop_table(name)
