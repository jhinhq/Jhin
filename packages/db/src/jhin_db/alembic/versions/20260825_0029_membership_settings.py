"""Per-membership settings, holding first-run onboarding state.

Additive. Every *existing* membership is backfilled to ``dismissed``: those
people already know their way around, and a guided tour that ambushes a
workspace mid-flight on the day of the upgrade is a bug, not a welcome. Only
memberships created from here on start at ``pending`` and see it.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = '{"onboarding": {"status": "dismissed", "last_step": null}}'


def _json_dict() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "workspace_membership",
        sa.Column(
            "settings_json",
            _json_dict(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # JSONB will not take a bare string literal, so the cast is required on
    # Postgres and unavailable on SQLite (where unit tests build the schema).
    cast = "::jsonb" if op.get_bind().dialect.name == "postgresql" else ""
    op.execute(sa.text(f"UPDATE workspace_membership SET settings_json = '{_BACKFILL}'{cast}"))


def downgrade() -> None:
    op.drop_column("workspace_membership", "settings_json")
