"""Measured pricing: rates derived from real spend, a refreshable price
catalog, and price provenance on profiles
(docs/architecture/models.md, "Where prices come from").

Purely additive.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_dict() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # Which source last wrote the profile's price. NULL (every existing row)
    # means unknown provenance and is treated as user-entered, so no
    # automatic refresh can overwrite a price this migration cannot vouch for.
    op.add_column("model_profile", sa.Column("price_source", sa.String(32), nullable=True))

    op.create_table(
        "model_observed_price",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("model_key", sa.String(200), nullable=False),
        sa.Column("input_cost_micros_per_million", sa.Integer(), nullable=True),
        sa.Column("output_cost_micros_per_million", sa.Integer(), nullable=True),
        sa.Column("blended_cost_micros_per_million", sa.Integer(), nullable=True),
        sa.Column("derivation", sa.String(32), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "sample_input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "sample_output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("sample_runs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "sample_cost_micros", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_observed_price"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_model_observed_price_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["model_provider.id"],
            name="fk_model_observed_price_provider_id_model_provider",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "provider_id", "model_key", name="uq_model_observed_price_provider_id_model_key"
        ),
    )
    op.create_index(
        "ix_model_observed_price_workspace_id", "model_observed_price", ["workspace_id"]
    )
    op.create_index("ix_model_observed_price_provider_id", "model_observed_price", ["provider_id"])

    op.create_table(
        "price_catalog_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("entries_json", _json_dict(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_price_catalog_snapshot"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_price_catalog_snapshot_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id", "source", name="uq_price_catalog_snapshot_workspace_id_source"
        ),
    )
    op.create_index(
        "ix_price_catalog_snapshot_workspace_id", "price_catalog_snapshot", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_catalog_snapshot_workspace_id", table_name="price_catalog_snapshot")
    op.drop_table("price_catalog_snapshot")
    op.drop_index("ix_model_observed_price_provider_id", table_name="model_observed_price")
    op.drop_index("ix_model_observed_price_workspace_id", table_name="model_observed_price")
    op.drop_table("model_observed_price")
    op.drop_column("model_profile", "price_source")
