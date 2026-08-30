"""The synced app/skill catalog index: ``catalog_version`` and ``catalog_entry``.

Three decisions are worth stating where the schema records them.

*Global, not workspace-scoped.* These are the first tables besides ``user``
and ``user_session`` with no ``workspace_id``. The catalog is public reference
data — a periodically refreshed index of MCP servers and agent skills — and a
per-workspace copy would repeat the same thousands of rows with no tenant
meaning attached. Nothing in it is connected until somebody connects it.

*Generation swap, not in-place refresh.* A sync writes a whole new
``catalog_version`` in ``loading`` state, then flips exactly one row to
``active`` in a single transaction. ``uq_catalog_version_active`` — a unique
index over ``status`` restricted to ``status = 'active'`` — makes "exactly one
active generation" a schema fact and serialises two syncs racing to swap.
Readers resolve the active version id first, so they always see one complete
generation.

*Full text behind a dialect guard.* On PostgreSQL a GIN
``to_tsvector('english', search_text)`` index is created, mirroring ``0016``.
The base schema is valid without it and SQLite tests never create it; search
degrades to ``ILIKE``, which at a few thousand rows costs nothing. ``pg_trgm``
is deliberately not introduced.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_dict() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _json_list() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "catalog_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_tag", sa.String(64), nullable=False),
        sa.Column("source_repo", sa.String(120), nullable=False, server_default=sa.text("''")),
        sa.Column("asset_url", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column("data_sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(12), nullable=False, server_default=sa.text("'loading'")),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("mcp_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skill_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("shard_cursor", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "sources_lock_json", _json_dict(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("error", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_version"),
        sa.UniqueConstraint(
            "release_tag",
            "data_sha256",
            name="uq_catalog_version_release_tag_data_sha256",
        ),
        sa.CheckConstraint(
            "status IN ('loading', 'active', 'superseded', 'failed')",
            name=op.f("ck_catalog_version_status"),
        ),
    )
    op.create_table(
        "catalog_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(240), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("slug", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("summary", sa.String(200), nullable=False, server_default=sa.text("''")),
        sa.Column("homepage", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column("docs_url", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column("trust_tier", sa.String(20), nullable=False, server_default=sa.text("'indexed'")),
        sa.Column("trust_rank", sa.SmallInteger(), nullable=False, server_default=sa.text("3")),
        sa.Column(
            "default_risk", sa.String(12), nullable=False, server_default=sa.text("'elevated'")
        ),
        sa.Column("popularity", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("icon", sa.String(32), nullable=False, server_default=sa.text("'mcp'")),
        sa.Column("connector_type", sa.String(32), nullable=True),
        sa.Column("mcp_url", sa.String(512), nullable=True),
        sa.Column("url_unverified", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("transport", sa.String(20), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("auth_hint", sa.String(10), nullable=False, server_default=sa.text("'bearer'")),
        sa.Column("auth_note", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("setup_note", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("stdio_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("publishable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("license", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("tags_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("alias_keys_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sources_json", _json_list(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "connector_config_json", _json_dict(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("mcp_json", _json_dict(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("skill_json", _json_dict(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_entry"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["catalog_version.id"],
            name="fk_catalog_entry_version_id_catalog_version",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "version_id",
            "canonical_key",
            name="uq_catalog_entry_version_id_canonical_key",
        ),
        sa.UniqueConstraint(
            "version_id",
            "kind",
            "slug",
            name="uq_catalog_entry_version_id_kind_slug",
        ),
        sa.CheckConstraint("kind IN ('mcp', 'skill')", name=op.f("ck_catalog_entry_kind")),
        sa.CheckConstraint(
            "trust_tier IN ('curated', 'registry_verified', 'smithery_verified', 'indexed')",
            name=op.f("ck_catalog_entry_trust_tier"),
        ),
        sa.CheckConstraint(
            "transport IN ('streamable_http', 'sse', 'unknown')",
            name=op.f("ck_catalog_entry_transport"),
        ),
        sa.CheckConstraint(
            "auth_hint IN ('none', 'bearer', 'header', 'oauth')",
            name=op.f("ck_catalog_entry_auth_hint"),
        ),
        sa.CheckConstraint(
            "default_risk IN ('read', 'write', 'elevated', 'destructive')",
            name=op.f("ck_catalog_entry_default_risk"),
        ),
        sa.CheckConstraint(
            "trust_rank BETWEEN 0 AND 3", name=op.f("ck_catalog_entry_trust_rank_range")
        ),
    )

    op.create_index(
        "ix_catalog_version_status_created",
        "catalog_version",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index(
        "uq_catalog_version_active",
        "catalog_version",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_catalog_entry_version_id", "catalog_entry", ["version_id"])
    op.create_index(
        "ix_catalog_entry_version_kind_rank",
        "catalog_entry",
        ["version_id", "kind", "trust_rank", sa.text("popularity DESC"), "canonical_key"],
    )
    op.create_index(
        "ix_catalog_entry_version_category", "catalog_entry", ["version_id", "category"]
    )
    op.create_index(
        "ix_catalog_entry_version_publishable", "catalog_entry", ["version_id", "publishable"]
    )
    op.create_index("ix_catalog_entry_version_slug", "catalog_entry", ["version_id", "slug"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_catalog_entry_search_fts ON catalog_entry "
            "USING GIN (to_tsvector('english', search_text))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_catalog_entry_search_fts")
    op.drop_index("ix_catalog_entry_version_slug", table_name="catalog_entry")
    op.drop_index("ix_catalog_entry_version_publishable", table_name="catalog_entry")
    op.drop_index("ix_catalog_entry_version_category", table_name="catalog_entry")
    op.drop_index("ix_catalog_entry_version_kind_rank", table_name="catalog_entry")
    op.drop_index("ix_catalog_entry_version_id", table_name="catalog_entry")
    op.drop_index("uq_catalog_version_active", table_name="catalog_version")
    op.drop_index("ix_catalog_version_status_created", table_name="catalog_version")
    op.drop_table("catalog_entry")
    op.drop_table("catalog_version")
