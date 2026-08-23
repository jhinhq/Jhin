"""Provider billing: an optional admin credential for spend reporting and a
user-entered prepaid credit amount on ``model_provider``. Purely additive.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_provider", sa.Column("admin_secret_id", sa.Uuid(), nullable=True))
    op.add_column(
        "model_provider", sa.Column("credits_loaded_micros", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_model_provider_admin_secret_id_secret",
        "model_provider",
        "secret",
        ["admin_secret_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_model_provider_admin_secret_id_secret", "model_provider", type_="foreignkey"
    )
    op.drop_column("model_provider", "credits_loaded_micros")
    op.drop_column("model_provider", "admin_secret_id")
