"""Version-bound Ghost editorial review.

Revision ID: 0050
Revises: 0049
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

BASELINE = (
    ("variables.read", {}),
    ("variables.write", {}),
    ("schedules.read", {}),
    ("schedules.manage", {}),
    ("ghost.connection.bind", {}),
    *(
        (name, {"variable_audience": True})
        for name in (
            "ghost.post.list",
            "ghost.post.read",
            "ghost.draft.create",
            "ghost.draft.update",
            "ghost.review.request",
            "ghost.review.decide",
            "ghost.post.publish",
        )
    ),
)


def _backfill_baseline() -> None:
    """Install new bounded abilities without replacing explicit grants/denies."""
    bind = op.get_bind()
    agent = sa.table("agent", sa.column("id", sa.Uuid()), sa.column("workspace_id", sa.Uuid()))
    grant = sa.table(
        "agent_capability_grant",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("agent_id", sa.Uuid()),
        sa.column("capability", sa.String()),
        sa.column("scope_json", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.column("effect", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    for capability, scope in BASELINE:
        existing = (
            sa.select(grant.c.id)
            .where(
                grant.c.agent_id == agent.c.id,
                grant.c.workspace_id == agent.c.workspace_id,
                grant.c.capability == capability,
            )
            .exists()
        )
        query = sa.select(agent.c.id, agent.c.workspace_id).where(~existing)
        rows = [
            {
                "id": new_uuid7(),
                "workspace_id": workspace_id,
                "agent_id": agent_id,
                "capability": capability,
                "scope_json": scope,
                "effect": "allow",
                "created_at": now,
                "updated_at": now,
            }
            for agent_id, workspace_id in bind.execute(query)
        ]
        if rows:
            bind.execute(grant.insert(), rows)


def upgrade() -> None:
    op.create_table(
        "ghost_editorial_review",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("post_id", sa.String(24), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("admin_url", sa.String(2000), nullable=False),
        sa.Column("provider_updated_at", sa.String(50), nullable=False),
        sa.Column("snapshot_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column(
            "author_agent_id",
            sa.Uuid(),
            sa.ForeignKey("agent.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "publisher_agent_id",
            sa.Uuid(),
            sa.ForeignKey("agent.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "work_review_id", sa.Uuid(), sa.ForeignKey("work_review.id", ondelete="SET NULL")
        ),
        sa.Column(
            "work_request_id", sa.Uuid(), sa.ForeignKey("work_request.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publication_tool_call_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "connection_id",
            "post_id",
            "revision",
            "publisher_agent_id",
            name="uq_ghost_review_revision_publisher",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','changes_requested',"
            "'stale','publishing','published','uncertain')",
            name="ghost_review_status",
        ),
    )
    for field in ("workspace_id", "connection_id", "publisher_agent_id", "status"):
        op.create_index(f"ix_ghost_editorial_review_{field}", "ghost_editorial_review", [field])
    _backfill_baseline()


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ghost_editorial_review)")):
        raise RuntimeError(
            "Retain editorial review history; restore a matching backup to roll back"
        )
    op.drop_table("ghost_editorial_review")
