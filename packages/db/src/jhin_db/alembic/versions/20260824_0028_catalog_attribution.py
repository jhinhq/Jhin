"""Licence attribution on cached price-catalog snapshots
(docs/architecture/models.md, "Refreshing the community catalog").

Split from `0027` rather than folded into it: `0027` had already been applied,
and editing an applied migration leaves every database that ran the earlier
version silently missing the column.

The LiteLLM price map is MIT-licensed, and MIT requires the copyright and
permission notice to travel with substantial portions of the material.
Caching the map is redistribution, so the notice is stored on the row itself
rather than only in documentation.

Purely additive.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_catalog_snapshot",
        sa.Column("attribution", sa.String(300), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("price_catalog_snapshot", "attribution")
