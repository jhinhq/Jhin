"""Skill category taxonomy: nullable ``category`` on ``skill``
(docs/architecture/skills.md).

Purely additive. ``NULL`` means "General" — the API and web layers coalesce
it for display; existing rows are left untouched by this migration rather
than being backfilled, matching how skills defaults are never retroactively
applied to existing workspaces elsewhere in this app.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill",
        sa.Column("category", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skill", "category")
