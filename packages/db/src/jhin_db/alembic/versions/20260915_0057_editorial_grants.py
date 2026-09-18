"""Install bounded editorial capabilities for existing agents, preserving explicit grants."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from jhin_domain import new_uuid7

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

BASELINE = (
    ("unsplash.connection.bind", {}),
    *(
        (name, {"variable_audience": True})
        for name in (
            "ghost.review.read",
            "ghost.assignment.create",
            "ghost.assignment.read",
            "ghost.assignment.revise",
            "ghost.assignment.cancel",
            "ghost.assignment.attach_evidence",
            "ghost.archive.sync",
            "ghost.archive.status",
            "ghost.archive.search",
            "ghost.archive.read",
            "unsplash.photos.search",
            "unsplash.photos.select",
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
    _backfill_baseline()


def downgrade() -> None:
    # Grants may have been edited by administrators; do not silently delete them.
    pass
