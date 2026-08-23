"""Free shape avatars: a brand-cube shape and palette color on ``agent``.

Purely additive; both columns are null for every existing agent (initials,
upload, or generated avatars are untouched).

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Both create_check_constraint and drop_constraint run this name through the
# metadata naming convention (ck_%(table_name)s_%(constraint_name)s), matching
# how 0017 created it (stored in Postgres as the doubled
# "ck_agent_ck_agent_avatar_kind_values").
_KIND_CONSTRAINT_NAME = "ck_agent_avatar_kind_values"


def upgrade() -> None:
    op.add_column("agent", sa.Column("avatar_shape", sa.String(length=32), nullable=True))
    op.add_column("agent", sa.Column("avatar_color", sa.String(length=16), nullable=True))
    # avatar_kind gains the free "shape" value.
    op.drop_constraint(_KIND_CONSTRAINT_NAME, "agent", type_="check")
    op.create_check_constraint(
        _KIND_CONSTRAINT_NAME,
        "agent",
        "avatar_kind IN ('initials', 'upload', 'generated', 'shape')",
    )


def downgrade() -> None:
    op.drop_constraint(_KIND_CONSTRAINT_NAME, "agent", type_="check")
    op.create_check_constraint(
        _KIND_CONSTRAINT_NAME,
        "agent",
        "avatar_kind IN ('initials', 'upload', 'generated')",
    )
    op.drop_column("agent", "avatar_color")
    op.drop_column("agent", "avatar_shape")
