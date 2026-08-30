"""The synced app/skill catalog index: ``catalog_version`` and ``catalog_entry``
(docs/architecture/catalog.md).

These are the first tables in the schema, besides ``user`` and
``user_session``, that carry no ``workspace_id``. That is deliberate: the
catalog is public reference data — a periodically refreshed index of MCP
servers and agent skills published upstream — not workspace content. Nothing
in it is connected to anything until somebody in a workspace connects it, and
a copy per workspace would be the same 5,000 rows repeated with no tenant
meaning attached.

A refresh never mutates rows a reader might be looking at. Each sync writes a
whole new ``catalog_version`` in ``loading`` state, fills it shard by shard
(``shard_cursor`` records how far it got, so an interrupted run resumes rather
than restarts), and only then flips exactly one row to ``active`` in a single
transaction. Readers resolve the active version id first and filter every
query on it, so they see one complete generation or the previous one, never a
half-loaded mixture. ``uq_catalog_version_active`` — a unique index over
``status`` restricted to ``status = 'active'`` — is what makes "exactly one"
a schema fact rather than a convention, and it serialises two syncs racing to
swap.

The 50 hand-curated built-in entries in :mod:`jhin_connectors.catalog` are
*not* stored here. They are merged in at read time, always sort first, and
their slugs are reserved against synced rows, so a crawled server can never
take the name of a reviewed one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import (
    JsonDict,
    JsonList,
    StdUuid,
    TimestampMixin,
    UtcDateTime,
    UuidPkMixin,
)


class CatalogVersion(Base, UuidPkMixin, TimestampMixin):
    """One generation of the catalog: a release tag plus the digest of the
    data archive it was built from.

    ``status`` walks ``loading`` → ``active`` → ``superseded``, or
    ``loading`` → ``failed`` when a sync dies mid-load. A failed row keeps its
    entries only until the next attempt at the same release, which resets it
    and starts over from an empty shard cursor.
    """

    __tablename__ = "catalog_version"
    __table_args__ = (
        UniqueConstraint(
            "release_tag",
            "data_sha256",
            name="uq_catalog_version_release_tag_data_sha256",
        ),
        CheckConstraint("status IN ('loading', 'active', 'superseded', 'failed')", name="status"),
        # Exactly one active generation, enforced by the database rather than
        # by whichever sync process happens to be swapping.
        Index(
            "uq_catalog_version_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_catalog_version_status_created", "status", text("created_at DESC")),
    )

    # The upstream release this generation came from, e.g. "2026.08.28".
    release_tag: Mapped[str] = mapped_column(String(64))
    # "owner/repo" of the catalog source; configurable per deployment.
    source_repo: Mapped[str] = mapped_column(String(120), default="", server_default=text("''"))
    asset_url: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    # sha256 of the verified data archive; half of the idempotency key.
    data_sha256: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # loading | active | superseded | failed
    status: Mapped[str] = mapped_column(
        String(12), default="loading", server_default=text("'loading'")
    )
    entry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    mcp_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    skill_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Last fully-loaded shard, e.g. "mcp/7f"; "" means nothing loaded yet.
    # A resumed sync skips every shard at or before this one.
    shard_cursor: Mapped[str] = mapped_column(String(16), default="", server_default=text("''"))
    # The upstream sources.lock: which registries were crawled, when, and how
    # much each contributed. Provenance, kept for the operator, not the agent.
    sources_lock_json: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, default=dict, server_default=text("'{}'")
    )
    # Bounded and catalog-text-free: a failure reason never relays upstream
    # prose into the database.
    error: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class CatalogEntry(Base, UuidPkMixin, TimestampMixin):
    """One indexed MCP server or agent skill within one generation.

    Every string on this row has already been redacted, stripped of control
    characters, and capped at ingest; the column widths are the second gate.
    Nothing here is ever placed in an agent prompt or a tool definition — the
    catalog is what a person browses, not what a model reads.
    """

    __tablename__ = "catalog_entry"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "canonical_key",
            name="uq_catalog_entry_version_id_canonical_key",
        ),
        UniqueConstraint(
            "version_id",
            "kind",
            "slug",
            name="uq_catalog_entry_version_id_kind_slug",
        ),
        CheckConstraint("kind IN ('mcp', 'skill')", name="kind"),
        CheckConstraint(
            "trust_tier IN "
            "('curated', 'registry_verified', 'smithery_verified', 'reviewed', 'indexed')",
            name="trust_tier",
        ),
        CheckConstraint("transport IN ('streamable_http', 'sse', 'unknown')", name="transport"),
        CheckConstraint("auth_hint IN ('none', 'bearer', 'header', 'oauth')", name="auth_hint"),
        CheckConstraint(
            "default_risk IN ('read', 'write', 'elevated', 'destructive')",
            name="default_risk",
        ),
        CheckConstraint("trust_rank BETWEEN 0 AND 4", name="trust_rank_range"),
        # The gallery's default ordering, index-covered end to end: filter by
        # generation and kind, then most-trusted first, then most-popular.
        Index(
            "ix_catalog_entry_version_kind_rank",
            "version_id",
            "kind",
            "trust_rank",
            text("popularity DESC"),
            "canonical_key",
        ),
        Index("ix_catalog_entry_version_category", "version_id", "category"),
        Index("ix_catalog_entry_version_publishable", "version_id", "publishable"),
        Index("ix_catalog_entry_version_slug", "version_id", "slug"),
    )

    version_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("catalog_version.id", ondelete="CASCADE"), index=True
    )
    # "mcp:<source>:<upstream id>" or "skill:<source>:<upstream id>" — stable
    # upstream identity, and the conflict target of the load upsert.
    canonical_key: Mapped[str] = mapped_column(String(240))
    # mcp | skill
    kind: Mapped[str] = mapped_column(String(8))
    # Display/connect handle; rewritten at ingest if it collides with a
    # built-in curated slug, so a synced row can never impersonate one.
    slug: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    # A one-line flattening of description for the card; <= 200 chars.
    summary: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    homepage: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    docs_url: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    # The upstream logo URL the icon proxy may fetch for this entry, already
    # held to the sync's two-shape allowlist at ingest; "" when the entry has
    # none. Never served to a browser — the proxy route is what readers see.
    icon_url: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))

    # curated | registry_verified | smithery_verified | reviewed | indexed,
    # and its 0..4 sort rank. Provenance, not observed behaviour.
    trust_tier: Mapped[str] = mapped_column(
        String(20), default="indexed", server_default=text("'indexed'")
    )
    trust_rank: Mapped[int] = mapped_column(SmallInteger, default=4, server_default=text("4"))
    # The risk floor a connection made from this entry starts at. Never
    # "read" and never "destructive": the catalog knows where a server came
    # from, not what its tools do.
    default_risk: Mapped[str] = mapped_column(
        String(12), default="elevated", server_default=text("'elevated'")
    )
    popularity: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    category: Mapped[str] = mapped_column(String(64))
    icon: Mapped[str] = mapped_column(String(32), default="mcp", server_default=text("'mcp'"))

    # Set when the entry maps onto an installed native connector; null when
    # the generic mcp connector is the only way to reach it.
    connector_type: Mapped[str | None] = mapped_column(String(32), default=None)
    mcp_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # True until somebody upstream actually reached the endpoint. Raises the
    # risk floor one step for every tier but curated.
    url_unverified: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # streamable_http | sse | unknown
    transport: Mapped[str] = mapped_column(
        String(20), default="unknown", server_default=text("'unknown'")
    )
    # none | bearer | header | oauth
    auth_hint: Mapped[str] = mapped_column(
        String(10), default="bearer", server_default=text("'bearer'")
    )
    auth_note: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    setup_note: Mapped[str] = mapped_column(String(500), default="", server_default=text("''"))
    # Local-process servers: listed for completeness, never connectable from
    # a hosted deployment.
    stdio_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Passed the upstream projection gate: complete enough to show as a
    # connectable app rather than a bare index row.
    publishable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    license: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))

    tags_json: Mapped[list[str]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    # Other canonical keys that resolve to this entry after upstream merging.
    alias_keys_json: Mapped[list[str]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    # [{"source_id", "upstream_id", "url"}, ...]
    sources_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonList, default=list, server_default=text("'[]'")
    )
    # Prefill values for known connector fields only. The field *definitions*
    # are always built server-side from installed manifests; catalog data
    # never describes a form.
    connector_config_json: Mapped[dict[str, str]] = mapped_column(
        JsonDict, default=dict, server_default=text("'{}'")
    )
    # Kind-specific detail, sanitized and byte-capped at ingest. Exactly one
    # of the two is populated; the other is {}.
    mcp_json: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, default=dict, server_default=text("'{}'")
    )
    skill_json: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, default=dict, server_default=text("'{}'")
    )
    # Lowercased "name slug category description tags", <= 2000 chars. On
    # PostgreSQL a GIN to_tsvector index covers it; elsewhere search falls
    # back to ILIKE, which is fine at this row count.
    search_text: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))


class CatalogIcon(Base, UuidPkMixin, TimestampMixin):
    """One cached logo, fetched by the icon proxy and served same-origin.

    Keyed by slug rather than by entry id on purpose: the slug is the identity
    a browser asks for, it survives generation swaps, and the resolution rules
    (builtin first, then the active generation) live in the proxy rather than
    in a foreign key that would pin the cache to one generation's rows.

    ``body`` only ever holds bytes the proxy's checks accepted — a raster
    image whose magic numbers matched one of the four recognised formats,
    read against a byte cap — never SVG and never a body served on the
    upstream header's say-so. ``status`` walks ``pending`` → ``ok`` or
    ``failed``; a failed fetch is cached too, so a dead upstream costs one
    request a week instead of one per page view.
    """

    __tablename__ = "catalog_icon"
    __table_args__ = (UniqueConstraint("slug", name="uq_catalog_icon_slug"),)

    # The display/connect handle whose card wants this logo.
    slug: Mapped[str] = mapped_column(String(32))
    # Where the body came from, re-validated against the shape allowlist at
    # fetch time; provenance for the operator, never served.
    source_url: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    content_type: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    body: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    # pending | ok | failed
    status: Mapped[str] = mapped_column(
        String(12), default="pending", server_default=text("'pending'")
    )
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
