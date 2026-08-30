"""Catalog search, facets, detail, and the trust-tier risk floor
(docs/architecture/catalog.md).

The library people browse is two sources merged into one list. The
hand-curated entries compiled into :mod:`jhin_connectors.catalog` are the apps
Jhin stands behind; everything else is whatever the sync job last indexed from
the public catalog. Curated entries always sort first — ahead of every synced
row, whatever the search scores — and their slugs are reserved, so a crawled
server can never take the name of a reviewed one. The sync already renames a
colliding slug; this module drops one anyway, because the second gate costs a
``NOT IN`` over fifty strings and being right once is not a plan.

Readers resolve the active generation first and filter every query on its id,
so a sync filling a new version is invisible until it swaps.

On sanitisation: every scalar column arrived through
:func:`jhin_catalog_sync.wire.clean_text` and is held to a column width, so a
search hit needs no second pass. The JSON columns have no width — their
strings are re-cleaned here, on the way out, and their lists are bounded by
count as well as by length.

Nothing here is ever read into an agent prompt or a tool definition. Catalog
text is provider-controlled prose whose only destination is a browser.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, cast
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import ColumnElement

from jhin_api.audit import service as audit
from jhin_api.catalog.config_schema import build_config_schema
from jhin_api.catalog.schemas import (
    AuthHintName,
    CatalogEntryDetailOut,
    CatalogEntryOut,
    CatalogFacetBucket,
    CatalogFacetsOut,
    CatalogKind,
    CatalogMcpDetailOut,
    CatalogSkillDetailOut,
    CatalogSourceOut,
    CatalogTier,
    CatalogVersionOut,
    ConfigSchemaOut,
    RiskFloorAppliedOut,
    RiskFloorApply,
    RiskName,
    TransportHint,
)
from jhin_api.deps import WorkspaceContext
from jhin_api.settings import get_settings
from jhin_catalog_sync.risk import DEFAULT_RISK_BY_TRUST, default_risk, risk_rank
from jhin_catalog_sync.wire import clean_text, safe_icon_url
from jhin_connectors.catalog import CatalogApp, load_catalog
from jhin_connectors.mcp import (
    MCP_CONNECTOR_TYPE,
    OVERRIDES_KEY,
    effective_risk,
    stored_overrides,
    stored_tools,
)
from jhin_connectors.mcp.discovery import is_valid_server_slug
from jhin_db.models import CatalogEntry, CatalogVersion, Connection
from jhin_policy import RiskLevel
from jhin_tools.sanitize import sanitize_payload

MAX_PAGE_SIZE: int = 100

# The one status a reader ever looks at. A ``loading`` generation is never
# active, which is the whole reason a half-filled sync is invisible.
_ACTIVE = "active"

# Built-in entries are curated by definition, so their tier, kind, risk floor
# and popularity are the same constant for all of them. That is also why they
# need no trust rank: the merge places the whole block ahead of the synced
# rows rather than sorting the two together.
_BUILTIN_TIER: CatalogTier = "curated"
_BUILTIN_KIND: CatalogKind = "mcp"
_BUILTIN_RISK: RiskName = DEFAULT_RISK_BY_TRUST[_BUILTIN_TIER].value
_BUILTIN_POPULARITY = 1.0

# Mirrors of the ingest-side caps, applied again to anything that reaches the
# wire out of a JSON column (§1.2 of the build spec).
_MAX_QUERY_CHARS = 120
_MAX_SEARCH_TEXT_CHARS = 2_000
_MAX_SUMMARY_CHARS = 200
_MAX_BLOB_STRING_CHARS = 512
_MAX_BLOB_DOCUMENT_BYTES = 8_192
_MAX_TAGS = 20
_MAX_TAG_CHARS = 40
_MAX_SOURCES = 10
_MAX_SOURCE_ID_CHARS = 120
_MAX_URL_CHARS = 512
_MAX_LIST_ITEMS = 20
_MAX_LIST_ITEM_CHARS = 200
_MAX_DETAIL_TEXT_CHARS = 200
_MAX_CONFIG_ENTRIES = 10
_MAX_CONFIG_KEY_CHARS = 64
_MAX_CONFIG_VALUE_CHARS = 500
_MAX_TOOL_COUNT = 10_000

_FACET_DIMENSIONS: tuple[str, ...] = ("kind", "category", "trust_tier", "transport", "auth_hint")

_FACET_COLUMNS: dict[str, InstrumentedAttribute[str]] = {
    "kind": CatalogEntry.kind,
    "category": CatalogEntry.category,
    "trust_tier": CatalogEntry.trust_tier,
    "transport": CatalogEntry.transport,
    "auth_hint": CatalogEntry.auth_hint,
}

# Chip labels. Deliberately shorter than the badge wording the web app uses
# for the same tiers: a filter chip names the group, a badge explains it.
_TRUST_LABELS: dict[str, str] = {
    "curated": "Curated by Jhin",
    "registry_verified": "Official registry",
    "smithery_verified": "Smithery verified",
    "reviewed": "Reviewed library",
    "indexed": "Community indexed",
}
_KIND_LABELS: dict[str, str] = {"mcp": "Apps", "skill": "Skills"}

# Protocol vocabulary, spelled the way a person reads it. `.title()` on these
# produced "Streamable Http" and "Oauth", which is machinery with bad casing
# rather than a label; anything genuinely unknown still falls through to the
# title-cased raw value so a new upstream value renders as *something*.
_TRANSPORT_LABELS: dict[str, str] = {
    "streamable_http": "Streamable HTTP",
    "sse": "SSE",
    "unknown": "Not stated",
}
_AUTH_LABELS: dict[str, str] = {
    "none": "No sign-in",
    "bearer": "API token",
    "header": "API key header",
    "oauth": "OAuth",
}


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


# --- the curated half -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Builtin:
    """One curated entry with the two derived values the merge needs: a stable
    sort key of the shape synced rows carry, and the same haystack the ingest
    builds for a synced row."""

    app: CatalogApp
    canonical_key: str
    search_text: str


@cache
def _builtins() -> tuple[_Builtin, ...]:
    # A production-like install never lists the dev stack's test doubles.
    # APP_ENV is process-constant, so caching the filtered tuple is safe.
    production_like = get_settings().is_production_like
    return tuple(
        _Builtin(
            app=app,
            canonical_key=f"mcp:builtin:{app.slug}",
            search_text=" ".join((app.name, app.slug, app.category, app.description)).lower()[
                :_MAX_SEARCH_TEXT_CHARS
            ],
        )
        for app in load_catalog()
        if not (app.dev_only and production_like)
    )


@cache
def _builtin_by_slug() -> Mapping[str, _Builtin]:
    return {item.app.slug: item for item in _builtins()}


@cache
def builtin_slugs() -> frozenset[str]:
    """Slugs of the hand-curated jhin_connectors entries. Reserved: a synced
    row carrying one of these is dropped at projection time as a second gate
    behind ``jhin_catalog_sync.wire.safe_slug``."""
    return frozenset(_builtin_by_slug())


@cache
def _reserved_slugs() -> tuple[str, ...]:
    """The reserved set as a sorted tuple, so the ``NOT IN`` renders the same
    SQL every time and stays comparable in a query log."""
    return tuple(sorted(builtin_slugs()))


# --- filters, shared by search and facets -----------------------------------


@dataclass(frozen=True, slots=True)
class _Filters:
    """The filter set, normalised once so search and facets cannot drift."""

    needle: str = ""
    kind: str | None = None
    category: str | None = None
    trust_tier: str | None = None
    transport: str | None = None
    auth_hint: str | None = None
    connectable: bool | None = None
    include_indexed: bool = False

    def without(self, dimension: str) -> _Filters:
        """The same filters with one faceted dimension released — what that
        dimension counts over, so a chip can still say what picking it would
        give. ``include_indexed`` is a separate switch and is never released,
        which is what keeps every dimension's buckets summing to the total."""
        return _Filters(
            needle=self.needle,
            kind=None if dimension == "kind" else self.kind,
            category=None if dimension == "category" else self.category,
            trust_tier=None if dimension == "trust_tier" else self.trust_tier,
            transport=None if dimension == "transport" else self.transport,
            auth_hint=None if dimension == "auth_hint" else self.auth_hint,
            connectable=self.connectable,
            include_indexed=self.include_indexed,
        )


def _filters(
    *,
    q: str | None,
    kind: str | None,
    category: str | None,
    trust_tier: str | None,
    transport: str | None,
    auth_hint: str | None,
    connectable: bool | None,
    include_indexed: bool,
) -> _Filters:
    return _Filters(
        needle=(q or "").strip().lower()[:_MAX_QUERY_CHARS],
        kind=(kind or "").strip() or None,
        category=(category or "").strip() or None,
        trust_tier=(trust_tier or "").strip() or None,
        transport=(transport or "").strip() or None,
        auth_hint=(auth_hint or "").strip() or None,
        connectable=connectable,
        include_indexed=include_indexed,
    )


def _escape_like(value: str) -> str:
    """Escape the LIKE metacharacters so a search for ``100%`` is a search for
    ``100%`` and not for everything."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _match_rank(needle: str) -> ColumnElement[int]:
    """How well a row matches, highest first: an exact name, then a name
    prefix, then a name substring, then the slug, then the haystack."""
    escaped = _escape_like(needle)
    return case(
        (func.lower(CatalogEntry.name) == needle, 100),
        (func.lower(CatalogEntry.name).like(f"{escaped}%", escape="\\"), 80),
        (func.lower(CatalogEntry.name).like(f"%{escaped}%", escape="\\"), 60),
        (func.lower(CatalogEntry.slug).like(f"%{escaped}%", escape="\\"), 40),
        (CatalogEntry.search_text.like(f"%{escaped}%", escape="\\"), 20),
        else_=0,
    )


def _fts_clause(db: AsyncSession, needle: str) -> ColumnElement[bool] | None:
    """The Postgres full-text arm of the match, or None everywhere else.

    Mirrors ``jhin_memory.retrieval._lexical_clause``: on Postgres the GIN
    index over ``to_tsvector('english', search_text)`` does the work, and on
    SQLite the LIKE ladder alone is what runs."""
    if db.get_bind().dialect.name != "postgresql":
        return None
    return func.to_tsvector("english", CatalogEntry.search_text).op("@@")(
        func.plainto_tsquery("english", needle)
    )


def _connectable_clause() -> ColumnElement[bool]:
    """The SQL twin of :func:`_connectable`, and it has to stay one: a filter
    that disagrees with the badge puts entries on the wrong side of
    ``?connectable=``. A synced row never carries a usable ``connector_type``
    (:data:`_SYNCED_CONNECTOR_TYPE`), so the only synced entry Connect can act
    on is a non-stdio MCP server."""
    return and_(CatalogEntry.kind == "mcp", CatalogEntry.stdio_only.is_(False))


def _conditions(
    version_id: UUID, filters: _Filters, *, db: AsyncSession
) -> list[ColumnElement[bool]]:
    """Every WHERE clause a synced read applies, generation gate first."""
    conditions: list[ColumnElement[bool]] = [
        CatalogEntry.version_id == version_id,
        # Gate 2 against slug theft: whatever the sync wrote, a curated slug
        # resolves to the curated entry and nothing else.
        CatalogEntry.slug.notin_(_reserved_slugs()),
        # The same gate on the tier. "curated" means a person at Jhin reviewed
        # the entry, so a synced row asserting it is making a claim only the
        # built-ins can make -- and one that pays, since `default_risk`
        # exempts curated from the unverified bump. The sync strips it at
        # ingest; a row that still carries it did not come from this sync and
        # is not served. Excluding it here rather than rewriting it on the way
        # out is what keeps each facet dimension's buckets summing to `total`.
        CatalogEntry.trust_tier != _BUILTIN_TIER,
    ]
    if filters.kind is not None:
        conditions.append(CatalogEntry.kind == filters.kind)
    if filters.category is not None:
        conditions.append(CatalogEntry.category == filters.category)
    if filters.trust_tier is not None:
        conditions.append(CatalogEntry.trust_tier == filters.trust_tier)
    if filters.transport is not None:
        conditions.append(CatalogEntry.transport == filters.transport)
    if filters.auth_hint is not None:
        conditions.append(CatalogEntry.auth_hint == filters.auth_hint)
    if not filters.include_indexed:
        conditions.append(CatalogEntry.trust_tier != "indexed")
    if filters.connectable is not None:
        clause = _connectable_clause()
        conditions.append(clause if filters.connectable else not_(clause))
    if filters.needle:
        match = _match_rank(filters.needle) > 0
        fts = _fts_clause(db, filters.needle)
        conditions.append(match if fts is None else or_(match, fts))
    return conditions


def _order(needle: str) -> list[ColumnElement[Any]]:
    order: list[ColumnElement[Any]] = []
    if needle:
        order.append(_match_rank(needle).desc())
    order.extend(
        (
            CatalogEntry.trust_rank.asc(),
            CatalogEntry.popularity.desc(),
            CatalogEntry.canonical_key.asc(),
        )
    )
    return order


def _match_score(item: _Builtin, needle: str) -> int:
    """The Python twin of :func:`_match_rank`, scoring a curated entry on the
    same ladder so one ordering rule covers both halves of the list."""
    name = item.app.name.lower()
    if name == needle:
        return 100
    if name.startswith(needle):
        return 80
    if needle in name:
        return 60
    if needle in item.app.slug:
        return 40
    if needle in item.search_text:
        return 20
    return 0


def _builtin_passes(item: _Builtin, filters: _Filters) -> bool:
    app = item.app
    if filters.kind is not None and filters.kind != _BUILTIN_KIND:
        return False
    if filters.category is not None and app.category != filters.category:
        return False
    if filters.trust_tier is not None and filters.trust_tier != _BUILTIN_TIER:
        return False
    if filters.transport is not None and app.transport != filters.transport:
        return False
    if filters.auth_hint is not None and app.auth_hint != filters.auth_hint:
        return False
    if filters.connectable is not None and app.connectable is not filters.connectable:
        return False
    # ``include_indexed`` cannot hide a curated entry, so it is not consulted.
    return not (filters.needle and _match_score(item, filters.needle) == 0)


def _builtin_hits(filters: _Filters) -> list[_Builtin]:
    """Matching curated entries, ordered. Trust rank and popularity are
    constant across them, so only the match score and the canonical key move."""
    hits = [item for item in _builtins() if _builtin_passes(item, filters)]
    if not filters.needle:
        return sorted(hits, key=lambda item: item.canonical_key)
    needle = filters.needle
    return sorted(hits, key=lambda item: (-_match_score(item, needle), item.canonical_key))


def _builtin_dimension(item: _Builtin, dimension: str) -> str:
    """Which bucket a curated entry falls into for one faceted dimension."""
    if dimension == "kind":
        return _BUILTIN_KIND
    if dimension == "trust_tier":
        return _BUILTIN_TIER
    if dimension == "category":
        return item.app.category
    if dimension == "transport":
        return item.app.transport
    return item.app.auth_hint


# --- projections ------------------------------------------------------------


def _connectable(*, kind: str, connector_type: str | None, stdio_only: bool) -> bool:
    """Whether the Connect button would do something.

    The same rule ``CatalogApp.connectable`` uses -- a native connector can
    always be reached, and a remote MCP endpoint can unless it is stdio-only
    -- with the kind gate that ``CatalogApp`` never needs because every
    curated entry is an MCP server. A skill is installed, not connected, so
    offering Connect on one promises something no code path delivers:
    :func:`_config_schema` correctly returns ``None`` for it, which would open
    a form with nothing in it.
    """
    if kind != "mcp":
        return False
    return connector_type is not None or not stdio_only


def _synced_tier(value: str) -> CatalogTier:
    """The trust tier a synced row is allowed to display.

    ``curated`` is Jhin's own word for "a person reviewed this", and
    :data:`_BUILTIN_TIER` is the only place it is true. The sync strips the
    claim at ingest (``jhin_catalog_sync.risk.syncable_tier``); this is the
    read-side twin, so a row written by an older sync -- or by anything that
    was not ``wire.to_row`` -- still cannot wear the badge.
    """
    return "indexed" if value == _BUILTIN_TIER else cast(CatalogTier, value)


def _synced_risk(row: CatalogEntry) -> RiskLevel:
    """The floor a synced row's provenance actually justifies.

    Recomputed from the sanitised tier rather than read back out of
    ``default_risk``, so a row that claimed ``curated`` cannot keep the floor
    that claim bought it -- :func:`default_risk` exempts ``curated`` from the
    unverified bump, so believing the stored column would hand an unreachable
    crawled endpoint ``write`` where it has earned ``elevated``.
    """
    return default_risk(_synced_tier(row.trust_tier), url_unverified=row.url_unverified)


#: Where a browser fetches an entry's logo: this server, never upstream. The
#: proxy behind the path re-validates the stored URL against its own
#: allowlist before dialling anything, so handing out the path asserts only
#: that an upstream URL exists, not that it will be honoured.
_ICON_PROXY_PATH: Final = "/api/v1/catalog/entries/{slug}/icon"


def _logo_url(slug: str, icon_url: str) -> str | None:
    """The same-origin proxy path for an entry's logo, or ``None`` when the
    entry has none. The stored upstream URL itself never leaves the server."""
    return _ICON_PROXY_PATH.format(slug=slug) if icon_url else None


def _builtin_icon_url(app: CatalogApp) -> str:
    """The curated entry's upstream logo URL, or "" when it ships none."""
    return app.icon_url


def builtin_logo_url(app: CatalogApp) -> str | None:
    """The same-origin proxy path for a curated entry's logo, or ``None``.
    What ``/api/v1/connectors/catalog`` publishes for each entry, so the two
    listings hand a browser the same path for the same logo."""
    return _logo_url(app.slug, _builtin_icon_url(app))


async def icon_source_url(db: AsyncSession, slug: str) -> str:
    """The upstream URL the icon proxy may dial for ``slug``, or "".

    Curated first, exactly like :func:`get_entry`, then the active
    generation's row. Both arms re-pass :func:`safe_icon_url` on the way out,
    so whatever wrote the stored column, the proxy only ever sees the two
    reviewed shapes — or nothing.
    """
    builtin = _builtin_by_slug().get(slug)
    if builtin is not None:
        return safe_icon_url(_builtin_icon_url(builtin.app))
    row = await _synced_by_slug(db, slug)
    return "" if row is None else safe_icon_url(row.icon_url)


#: The native connector a synced row is allowed to name: none.
#:
#: A crawled entry is an MCP server, and ``config_schema._manifest_for``
#: resolves a ``connector_type`` against the *installed* registry -- so a
#: synced row naming ``github`` would render GitHub's real Connect form,
#: GitHub's auth schemes and all, under a name and icon the index chose, and
#: ``connectionsForApp`` would then badge it "Connected" off an unrelated
#: GitHub connection. Slug theft gets three gates; this is the same theft
#: through a different column. Serving it as generic MCP is what
#: ``_manifest_for`` already documents as the graceful path.
_SYNCED_CONNECTOR_TYPE: Final[str | None] = None


def _builtin_out(item: _Builtin) -> CatalogEntryOut:
    app = item.app
    return CatalogEntryOut(
        slug=app.slug,
        kind=_BUILTIN_KIND,
        source="builtin",
        name=app.name,
        summary=app.description[:_MAX_SUMMARY_CHARS],
        category=app.category,
        icon=app.icon,
        logo_url=_logo_url(app.slug, _builtin_icon_url(app)),
        trust_tier=_BUILTIN_TIER,
        default_risk=_BUILTIN_RISK,
        popularity=_BUILTIN_POPULARITY,
        connector_type=app.connector_type,
        mcp_url=app.mcp_url,
        url_unverified=app.url_unverified,
        transport=app.transport,
        auth_hint=app.auth_hint,
        stdio_only=app.stdio_only,
        deprecated=False,
        connectable=app.connectable,
        docs_url=app.docs_url,
    )


def _entry_out(row: CatalogEntry) -> CatalogEntryOut:
    return CatalogEntryOut(
        slug=row.slug,
        kind=cast(CatalogKind, row.kind),
        source="synced",
        name=row.name,
        summary=row.summary,
        category=row.category,
        icon=row.icon,
        logo_url=_logo_url(row.slug, row.icon_url),
        trust_tier=_synced_tier(row.trust_tier),
        default_risk=_synced_risk(row).value,
        popularity=row.popularity,
        connector_type=_SYNCED_CONNECTOR_TYPE,
        mcp_url=_external_url(row.mcp_url) or None,
        url_unverified=row.url_unverified,
        transport=cast(TransportHint, row.transport),
        auth_hint=cast(AuthHintName, row.auth_hint),
        stdio_only=row.stdio_only,
        deprecated=row.deprecated,
        connectable=_connectable(
            kind=row.kind,
            connector_type=_SYNCED_CONNECTOR_TYPE,
            stdio_only=row.stdio_only,
        ),
        docs_url=_external_url(row.docs_url),
    )


def _version_out(version: CatalogVersion) -> CatalogVersionOut:
    return CatalogVersionOut(
        release_tag=version.release_tag,
        source_repo=version.source_repo,
        data_sha256=version.data_sha256,
        entry_count=version.entry_count,
        mcp_count=version.mcp_count,
        skill_count=version.skill_count,
        activated_at=version.activated_at,
    )


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_blob(raw: object) -> dict[str, Any]:
    """Second pass of the ingest-time sanitisation over a JSON column.

    ``sanitize_payload`` redacts anything this process knows to be a secret
    and caps both the leaves and the document; when the document is over the
    cap it hands back its own marker object, whose keys none of the readers
    below recognise, so an oversized blob degrades to empty detail rather than
    to a large response."""
    if not isinstance(raw, dict) or not raw:
        return {}
    return sanitize_payload(
        raw,
        max_string_chars=_MAX_BLOB_STRING_CHARS,
        max_document_bytes=_MAX_BLOB_DOCUMENT_BYTES,
    )


def _strings(raw: object, *, limit: int, max_chars: int) -> list[str]:
    """A JSON list column as a bounded list of clean strings."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if len(out) >= limit:
            break
        value = clean_text(_as_str(item), max_chars=max_chars)
        if value:
            out.append(value)
    return out


def _field_values(
    raw: object, key: str, *, limit: int, clean: Callable[[object], str]
) -> list[str]:
    """One field pulled out of a bounded list of JSON objects, each value put
    through ``clean`` — which is where a value that will be rendered as a link
    gets a stricter reading than one that will be rendered as text."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if len(out) >= limit:
            break
        if not isinstance(item, dict):
            continue
        value = clean(item.get(key))
        if value:
            out.append(value)
    return out


def _bounded_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_TOOL_COUNT else None


def _list_item(value: object) -> str:
    return clean_text(_as_str(value), max_chars=_MAX_LIST_ITEM_CHARS)


def _external_url(value: object) -> str:
    """A URL out of a JSON column, or "" when it is not one we would ever show.
    The browser gates rendering as well (https only); a non-web scheme dies
    here so it never becomes a string anybody has to think about."""
    url = clean_text(_as_str(value), max_chars=_MAX_URL_CHARS)
    return url if url.startswith(("https://", "http://")) else ""


def _connector_config(raw: object) -> dict[str, str]:
    """Prefill values for known connector fields. These are values only —
    ``config_schema.build_config_schema`` decides which fields exist, and it
    ignores every key it does not already know."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_CONFIG_ENTRIES:
            break
        name = clean_text(_as_str(key), max_chars=_MAX_CONFIG_KEY_CHARS)
        if not name:
            continue
        out[name] = clean_text(_as_str(value), max_chars=_MAX_CONFIG_VALUE_CHARS)
    return out


def _sources(raw: object) -> list[CatalogSourceOut]:
    if not isinstance(raw, list):
        return []
    out: list[CatalogSourceOut] = []
    for item in raw:
        if len(out) >= _MAX_SOURCES:
            break
        if not isinstance(item, dict):
            continue
        out.append(
            CatalogSourceOut(
                source_id=clean_text(
                    _as_str(item.get("source_id")), max_chars=_MAX_SOURCE_ID_CHARS
                ),
                upstream_id=clean_text(
                    _as_str(item.get("upstream_id")), max_chars=_MAX_SOURCE_ID_CHARS
                ),
                url=_external_url(item.get("url")),
            )
        )
    return out


def _mcp_detail(blob: Mapping[str, Any]) -> CatalogMcpDetailOut:
    return CatalogMcpDetailOut(
        tool_count=_bounded_int(blob.get("tool_count")),
        registry_name=clean_text(
            _as_str(blob.get("registry_name")), max_chars=_MAX_DETAIL_TEXT_CHARS
        ),
        npm_package=clean_text(_as_str(blob.get("npm_package")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        verified_upstream=blob.get("verified_upstream") is True,
        package_identifiers=_field_values(
            blob.get("packages"), "identifier", limit=_MAX_LIST_ITEMS, clean=_list_item
        ),
        remote_urls=_field_values(
            blob.get("remotes"), "url", limit=_MAX_LIST_ITEMS, clean=_external_url
        ),
    )


def _skill_detail(blob: Mapping[str, Any]) -> CatalogSkillDetailOut:
    raw_plugin = blob.get("plugin")
    plugin: Mapping[str, Any] = raw_plugin if isinstance(raw_plugin, dict) else {}
    return CatalogSkillDetailOut(
        skill_name=clean_text(_as_str(blob.get("skill_name")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        source_ref=clean_text(_as_str(blob.get("source_ref")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        skill_path=clean_text(_as_str(blob.get("skill_path")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        commit_sha=clean_text(_as_str(blob.get("commit_sha")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        marketplace=clean_text(
            _as_str(plugin.get("marketplace")), max_chars=_MAX_DETAIL_TEXT_CHARS
        ),
        plugin=clean_text(_as_str(plugin.get("plugin")), max_chars=_MAX_DETAIL_TEXT_CHARS),
        model_invocable=blob.get("model_invocable") is not False,
        allowed_tools=_strings(
            blob.get("allowed_tools"), limit=_MAX_LIST_ITEMS, max_chars=_MAX_LIST_ITEM_CHARS
        ),
    )


def _config_schema(
    base: CatalogEntryOut,
    *,
    auth_note: str,
    connector_config: Mapping[str, str],
) -> ConfigSchemaOut | None:
    """The Connect form's render contract, for entries a Connect button would
    do something with. A skill is installed, not connected, and a stdio-only
    server cannot be reached from a hosted deployment; neither gets a form."""
    if base.kind != "mcp" or not base.connectable:
        return None
    return build_config_schema(
        connector_type=base.connector_type,
        slug=base.slug,
        mcp_url=base.mcp_url,
        url_unverified=base.url_unverified,
        transport=base.transport,
        auth_hint=base.auth_hint,
        auth_note=auth_note,
        connector_config=connector_config,
    )


def _builtin_detail(item: _Builtin) -> CatalogEntryDetailOut:
    """A curated entry in full. ``mcp`` stays null: registry provenance is
    something the crawl discovers, and a reviewed entry did not come from one."""
    app = item.app
    base = _builtin_out(item)
    connector_config = dict(app.connector_config)
    return CatalogEntryDetailOut(
        **base.model_dump(),
        description=app.description,
        homepage="",
        auth_note=app.auth_note,
        setup_note=app.setup_note,
        license="",
        tags=[],
        connector_config=connector_config,
        sources=[],
        config_schema=_config_schema(
            base, auth_note=app.auth_note, connector_config=connector_config
        ),
        mcp=None,
        skill=None,
    )


def _synced_detail(row: CatalogEntry) -> CatalogEntryDetailOut:
    base = _entry_out(row)
    connector_config = _connector_config(row.connector_config_json)
    return CatalogEntryDetailOut(
        **base.model_dump(),
        description=row.description,
        homepage=_external_url(row.homepage),
        auth_note=row.auth_note,
        setup_note=row.setup_note,
        license=row.license,
        tags=_strings(row.tags_json, limit=_MAX_TAGS, max_chars=_MAX_TAG_CHARS),
        connector_config=connector_config,
        sources=_sources(row.sources_json),
        config_schema=_config_schema(
            base, auth_note=row.auth_note, connector_config=connector_config
        ),
        mcp=_mcp_detail(_safe_blob(row.mcp_json)) if row.kind == "mcp" else None,
        skill=_skill_detail(_safe_blob(row.skill_json)) if row.kind == "skill" else None,
    )


# --- the active generation --------------------------------------------------


async def _active_version_row(db: AsyncSession) -> CatalogVersion | None:
    """The generation being served, or None before the first sync has run.

    A ``loading`` row is never active, so resolving this first is what makes a
    half-filled sync invisible to every read below."""
    row: CatalogVersion | None = await db.scalar(
        select(CatalogVersion).where(CatalogVersion.status == _ACTIVE)
    )
    return row


async def active_version_id(db: AsyncSession) -> UUID | None:
    """Just the id, for the reads that only need the generation gate."""
    version_id: UUID | None = await db.scalar(
        select(CatalogVersion.id).where(CatalogVersion.status == _ACTIVE)
    )
    return version_id


async def active_version(db: AsyncSession) -> CatalogVersionOut | None:
    """What the library footer reports: which release is indexed, and how big."""
    row = await _active_version_row(db)
    return None if row is None else _version_out(row)


# --- search -----------------------------------------------------------------


async def search_entries(
    db: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    trust_tier: str | None = None,
    transport: str | None = None,
    auth_hint: str | None = None,
    connectable: bool | None = None,
    include_indexed: bool = False,
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[CatalogEntryOut], int, CatalogVersionOut | None]:
    """One page of the library, curated entries first, plus the total and the
    generation the synced half came from.

    The two halves are paged as one list: the page takes what it can from the
    curated block and asks the database only for the remainder, so an entry
    appears exactly once however the window falls across the boundary."""
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    offset = max(offset, 0)
    filters = _filters(
        q=q,
        kind=kind,
        category=category,
        trust_tier=trust_tier,
        transport=transport,
        auth_hint=auth_hint,
        connectable=connectable,
        include_indexed=include_indexed,
    )

    builtin_hits = _builtin_hits(filters)
    builtin_total = len(builtin_hits)
    page_builtin = builtin_hits[offset : offset + limit]
    items = [_builtin_out(item) for item in page_builtin]

    version = await _active_version_row(db)
    if version is None:
        return items, builtin_total, None

    query = select(CatalogEntry).where(*_conditions(version.id, filters, db=db))
    db_total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    db_limit = limit - len(page_builtin)
    if db_limit > 0:
        rows = await db.scalars(
            query.order_by(*_order(filters.needle))
            .limit(db_limit)
            .offset(max(0, offset - builtin_total))
        )
        items.extend(_entry_out(row) for row in rows)
    return items, builtin_total + db_total, _version_out(version)


# --- facets -----------------------------------------------------------------


def _facet_label(dimension: str, value: str) -> str:
    if dimension == "trust_tier":
        return _TRUST_LABELS.get(value, value)
    if dimension == "kind":
        return _KIND_LABELS.get(value, value)
    if dimension == "category":
        return value
    if dimension == "transport":
        return _TRANSPORT_LABELS.get(value, value.replace("_", " ").title())
    if dimension == "auth_hint":
        return _AUTH_LABELS.get(value, value.replace("_", " ").title())
    return value.replace("_", " ").title()


def _buckets(dimension: str, counts: Counter[str]) -> list[CatalogFacetBucket]:
    return [
        CatalogFacetBucket(value=value, label=_facet_label(dimension, value), count=count)
        for value, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


async def facets(
    db: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    trust_tier: str | None = None,
    transport: str | None = None,
    auth_hint: str | None = None,
    connectable: bool | None = None,
    include_indexed: bool = False,
) -> CatalogFacetsOut:
    """Counts for every filter chip, over both halves of the library.

    A dimension is counted with its own selection released, so the chips next
    to the one you picked still say what picking them instead would give.
    ``include_indexed`` is a separate switch and stays applied everywhere,
    which is what keeps each dimension's buckets summing to ``total``."""
    filters = _filters(
        q=q,
        kind=kind,
        category=category,
        trust_tier=trust_tier,
        transport=transport,
        auth_hint=auth_hint,
        connectable=connectable,
        include_indexed=include_indexed,
    )
    version = await _active_version_row(db)

    buckets: dict[str, list[CatalogFacetBucket]] = {}
    for dimension in _FACET_DIMENSIONS:
        relaxed = filters.without(dimension)
        counts: Counter[str] = Counter(
            _builtin_dimension(item, dimension)
            for item in _builtins()
            if _builtin_passes(item, relaxed)
        )
        if version is not None:
            column = _FACET_COLUMNS[dimension]
            rows = await db.execute(
                select(column, func.count())
                .where(*_conditions(version.id, relaxed, db=db))
                .group_by(column)
            )
            for value, count in rows.all():
                counts[str(value)] += int(count)
        buckets[dimension] = _buckets(dimension, counts)

    total = sum(1 for item in _builtins() if _builtin_passes(item, filters))
    if version is not None:
        query = select(CatalogEntry).where(*_conditions(version.id, filters, db=db))
        total += int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)

    return CatalogFacetsOut(
        kind=buckets["kind"],
        category=buckets["category"],
        trust_tier=buckets["trust_tier"],
        transport=buckets["transport"],
        auth_hint=buckets["auth_hint"],
        total=total,
    )


# --- detail -----------------------------------------------------------------


async def _synced_by_slug(db: AsyncSession, slug: str) -> CatalogEntry | None:
    """The active generation's row for ``slug``, never a reserved one.

    A slug is unique per (generation, kind), so an mcp server and a skill may
    share one. The most trusted wins, then the most popular, then the lowest
    canonical key — the same tie-break the listing orders by, so detail and
    search never disagree about which row a slug means."""
    version_id = await active_version_id(db)
    if version_id is None:
        return None
    row: CatalogEntry | None = await db.scalar(
        select(CatalogEntry)
        .where(
            CatalogEntry.version_id == version_id,
            CatalogEntry.slug == slug,
            CatalogEntry.slug.notin_(_reserved_slugs()),
            # Same tier gate the listing applies, so detail, search and the
            # risk floor cannot disagree about which rows exist.
            CatalogEntry.trust_tier != _BUILTIN_TIER,
        )
        .order_by(
            CatalogEntry.trust_rank.asc(),
            CatalogEntry.popularity.desc(),
            CatalogEntry.canonical_key.asc(),
        )
        .limit(1)
    )
    return row


async def get_entry(db: AsyncSession, slug: str) -> CatalogEntryDetailOut:
    """One entry in full. Curated first: a reserved slug always resolves to
    the entry Jhin reviewed, whatever a synced row claims."""
    if not is_valid_server_slug(slug):
        raise _not_found("Catalog entry")
    builtin = _builtin_by_slug().get(slug)
    if builtin is not None:
        return _builtin_detail(builtin)
    row = await _synced_by_slug(db, slug)
    if row is None:
        raise _not_found("Catalog entry")
    return _synced_detail(row)


# --- the trust-tier risk floor ----------------------------------------------


async def _risk_floor(db: AsyncSession, slug: str) -> RiskLevel:
    """The floor an entry's provenance justifies. Curated and registry-listed
    servers land on ``write`` (auto); anything crawled or with an unconfirmed
    endpoint lands on ``elevated``, which the policy engine turns into an
    approval prompt. Never ``read``, never ``destructive``: the catalog knows
    where a server came from, not what its tools do."""
    if not is_valid_server_slug(slug):
        raise _not_found("Catalog entry")
    if slug in builtin_slugs():
        return DEFAULT_RISK_BY_TRUST[_BUILTIN_TIER]
    row = await _synced_by_slug(db, slug)
    if row is None:
        raise _not_found("Catalog entry")
    return _synced_risk(row)


async def apply_risk_floor(
    db: AsyncSession,
    ctx: WorkspaceContext,
    body: RiskFloorApply,
    *,
    request_id: UUID,
    ip_hash: str,
) -> RiskFloorAppliedOut:
    """Raise every tool on one MCP connection to the floor its catalog entry
    justifies.

    This writes the same ``tool_risk_overrides`` key the per-tool editor
    writes and the tool worker already reads, so nothing in the connections
    module, the policy engine, or the gateway has to know the catalog exists.
    A tool already at or above the floor is left exactly as it is, which makes
    the action idempotent and means it can never lower a risk an admin
    raised."""
    connection = await db.scalar(
        select(Connection).where(
            Connection.id == body.connection_id,
            Connection.workspace_id == ctx.workspace_id,
        )
    )
    if connection is None:
        raise _not_found("Connection")
    if connection.connector_type != MCP_CONNECTOR_TYPE:
        raise _bad_request("Only MCP connections have per-tool risk overrides")
    tools = stored_tools(connection.config_json)
    if not tools:
        raise _bad_request("This connection has no discovered tools yet")

    floor = await _risk_floor(db, body.slug)
    current = stored_overrides(connection.config_json)
    overrides = {slug: risk.value for slug, risk in current.items()}
    raised = 0
    for tool in tools:
        if risk_rank(effective_risk(tool, current)) < risk_rank(floor):
            overrides[tool.slug] = floor.value
            raised += 1

    connection.config_json = {**connection.config_json, OVERRIDES_KEY: overrides}
    audit.record(
        db,
        action="catalog.risk_floor_applied",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"slug": body.slug, "floor": floor.value, "tools_raised": raised},
    )
    await db.commit()
    return RiskFloorAppliedOut(
        connection_id=connection.id,
        floor=floor.value,
        tools_raised=raised,
        tools_unchanged=len(tools) - raised,
    )
