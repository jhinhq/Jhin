"""Record which authority chose an editorial cover photo.

Revision ID: 0058
Revises: 0057
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every row that exists was chosen by a person: that was the only mode.
    op.add_column(
        "editorial_asset",
        sa.Column("selection_mode", sa.String(16), nullable=False, server_default="human"),
    )
    op.add_column(
        "editorial_asset",
        sa.Column(
            "selection_authority_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("editorial_asset", "selection_authority_json")
    op.drop_column("editorial_asset", "selection_mode")
