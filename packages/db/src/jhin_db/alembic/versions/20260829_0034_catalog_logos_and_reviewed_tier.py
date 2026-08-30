"""Catalog logos and the reviewed skill tier: ``icon_url``, ``catalog_icon``,
and a five-value trust vocabulary.

Three related changes land together because they ship one feature.

*``catalog_entry.icon_url``.* The sync now stores the upstream logo URL an
entry may carry — already reduced at ingest to the two shapes the icon proxy
is allowed to dial, or "". The raw URL never reaches a browser; the proxy
route is what readers see.

*``catalog_icon``.* The proxy's cache: one sanitised icon body per slug,
global like the rest of the catalog. ``status`` walks ``pending`` → ``ok`` or
``failed``, and a failure is cached too, so a dead upstream costs one request
a week rather than one per page view.

*The ``reviewed`` tier.* Skills from marketplaces the Jhin team reviewed sit
between ``smithery_verified`` and ``indexed``. That widens both the tier
vocabulary and the rank range: ``indexed`` moves from rank 3 to rank 4, so the
``trust_rank`` check and its server default move with it. The downgrade folds
``reviewed`` rows back into ``indexed`` before restoring the narrower checks —
restoring a four-value check over five-value data would refuse to apply.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("catalog_entry") as batch:
        batch.add_column(
            sa.Column("icon_url", sa.String(512), nullable=False, server_default=sa.text("''"))
        )
        batch.drop_constraint("trust_tier", type_="check")
        batch.create_check_constraint(
            "trust_tier",
            "trust_tier IN ('curated', 'registry_verified', 'smithery_verified', 'reviewed', "
            "'indexed')",
        )
        # ``indexed`` now ranks 4; the range check and the "least trusted by
        # default" server default both follow it.
        batch.drop_constraint("trust_rank_range", type_="check")
        batch.create_check_constraint("trust_rank_range", "trust_rank BETWEEN 0 AND 4")
        batch.alter_column(
            "trust_rank",
            existing_type=sa.SmallInteger(),
            server_default=sa.text("4"),
            existing_nullable=False,
        )
    op.create_table(
        "catalog_icon",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(32), nullable=False),
        sa.Column("source_url", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column("content_type", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("body", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_icon"),
        sa.UniqueConstraint("slug", name="uq_catalog_icon_slug"),
    )


def downgrade() -> None:
    op.drop_table("catalog_icon")
    # The narrower checks are about to be restored over live rows: demote what
    # the five-value vocabulary allowed before the four-value one refuses it.
    op.execute("UPDATE catalog_entry SET trust_tier = 'indexed' WHERE trust_tier = 'reviewed'")
    op.execute("UPDATE catalog_entry SET trust_rank = 3 WHERE trust_rank > 3")
    with op.batch_alter_table("catalog_entry") as batch:
        batch.alter_column(
            "trust_rank",
            existing_type=sa.SmallInteger(),
            server_default=sa.text("3"),
            existing_nullable=False,
        )
        batch.drop_constraint("trust_rank_range", type_="check")
        batch.create_check_constraint("trust_rank_range", "trust_rank BETWEEN 0 AND 3")
        batch.drop_constraint("trust_tier", type_="check")
        batch.create_check_constraint(
            "trust_tier",
            "trust_tier IN ('curated', 'registry_verified', 'smithery_verified', 'indexed')",
        )
        batch.drop_column("icon_url")
