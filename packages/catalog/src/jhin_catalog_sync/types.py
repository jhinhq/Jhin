"""Wire models: what the sync will accept from upstream, and what it will not.

The models are deliberately permissive about *unknown* fields and strict about
*known* ones. A catalog published by a newer generator must still load — an
index that stops refreshing because upstream added a column is worse than one
that ignores the column — but nothing outside the declared vocabularies is
allowed to reach a database column or a rendered badge.

Everything here is a value, never a scratchpad: the models are frozen, so the
projection onto a row (:mod:`jhin_catalog_sync.wire`) builds a new value
rather than editing what upstream said.

The failure family is one type with three specialisations. Callers catch
:class:`CatalogSyncError`; the CLI catches the three so it can map them onto
distinct exit codes. Every message is display-safe by contract — a stable
string that never carries a URL, a header, or a byte of upstream text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_SCHEMA_VERSION: Final = 1

EntryKind = Literal["mcp", "skill"]
TrustTier = Literal["curated", "registry_verified", "smithery_verified", "reviewed", "indexed"]
TransportHint = Literal["streamable_http", "sse", "unknown"]
AuthHint = Literal["none", "bearer", "header", "oauth"]

# "<kind>:<source>:<upstream id>", bounded to the ``canonical_key`` column
# width as a whole. The tail's character class is what keeps a crafted key
# out of a shard path or a log line: no spaces, no controls, ASCII only.
# ``#`` separates a monorepo subpath from its repository in a repo-derived
# key (``…/x402#packages/mcp-rollforge``). The index publishes any printable
# ASCII here; this stays an allowlist rather than adopting that, and was
# widened to exactly the one character the published data actually uses —
# measured across all 1,225 records of data-2026-08-29-1.
CANONICAL_KEY_RE: re.Pattern[str] = re.compile(
    r"^(?=.{1,240}$)(?:mcp|skill):[a-z0-9_]{1,32}:[A-Za-z0-9._@/+~#-]{1,220}$"
)
# The display/connect handle, and the ``slug`` column width.
SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9_]{1,32}$")


def shard_for(canonical_key: str) -> str:
    """Which shard a record belongs in: the first two hex digits of the
    sha256 of its canonical key.

    Upstream files records this way and the parser checks it, so a record
    cannot be smuggled into a shard the loader has already passed.
    """
    return hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:2]


class CatalogSyncError(Exception):
    """A catalog refresh could not complete. The message is display-safe."""


class CatalogIntegrityError(CatalogSyncError):
    """The archive did not match the digest published alongside it."""


class CatalogFetchError(CatalogSyncError):
    """The release or one of its assets could not be retrieved."""


class CatalogFormatError(CatalogSyncError):
    """The archive, or something inside it, was not the expected shape."""


class _Wire(BaseModel):
    """Frozen, extra-tolerant base for everything that arrives over the wire."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class RepoRef(_Wire):
    host: str = ""
    owner: str = ""
    repo: str = ""
    subpath: str = ""


class EntrySource(_Wire):
    """Where one upstream crawl found this entry."""

    source_id: str = ""
    upstream_id: str = ""
    url: str = ""


class PackageRef(_Wire):
    """A published package that carries the server. ``transport`` here is the
    package's own hint (``stdio`` among them) and is deliberately not the
    closed remote-transport vocabulary."""

    registry_type: str = ""
    identifier: str = ""
    version: str = ""
    runtime_hint: str = ""
    transport: str = ""


class RemoteRef(_Wire):
    transport: str = ""
    url: str = ""
    templated: bool = False
    header_names: tuple[str, ...] = ()


class PopularitySignals(_Wire):
    github_stars: int | None = None
    github_forks: int | None = None
    npm_downloads_monthly: int | None = None
    npm_dependents: int | None = None
    smithery_use_count: int | None = None
    registry_version_count: int | None = None


class PluginRef(_Wire):
    plugin: str = ""
    marketplace: str = ""


class CatalogRecord(_Wire):
    """The fields every indexed entry carries, whatever kind it is."""

    # Kept as upstream sent it: the shard parser refuses a future version per
    # record, so the value has to survive validation rather than be validated
    # away.
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    canonical_key: str
    kind: EntryKind
    slug: str
    name: str
    description: str = ""
    homepage: str = ""
    docs_url: str = ""
    # Not a closed vocabulary at the model: an entry filed under a category or
    # icon this build does not know is indexed but not publishable
    # (``wire.is_publishable``), which is softer than refusing the row.
    category: str = ""
    icon: str = "mcp"
    # An upstream logo URL the icon proxy may fetch, or "". Accepted here as
    # any string — the projection (``wire.safe_icon_url``) is the gate that
    # decides whether it matches the two shapes the producer is allowed to
    # emit, and blanks everything else.
    icon_url: str = ""
    # Set by the producer for a skill that came from a marketplace on the
    # reviewed allowlist. Old consumers ignore the field (``extra="ignore"``
    # cuts both ways); this one turns it into the ``reviewed`` tier at
    # projection time rather than trusting a tier upstream asserts.
    marketplace_reviewed: bool = False
    trust_tier: TrustTier = "indexed"
    popularity: float = 0.0
    tags: tuple[str, ...] = ()
    alias_keys: tuple[str, ...] = ()
    sources: tuple[EntrySource, ...] = ()
    license: str = ""
    deprecated: bool = False
    repo: RepoRef = RepoRef()

    @field_validator("repo", mode="before")
    @classmethod
    def _absent_repo(cls, value: object) -> object:
        """``repo: null`` is how the index says "hosted, with no source repo".

        A third of the published records are that: a Smithery-hosted remote
        has an endpoint and no repository behind it. The wire accepts the
        null and the model still presents a ``RepoRef``, so no reader has to
        learn about ``None``.
        """
        return RepoRef() if value is None else value

    # Checked here rather than through ``Field(pattern=...)``: both patterns
    # are anchored, and one uses a lookahead to bound the whole key, which the
    # default regex engine behind ``pattern`` does not implement.
    @field_validator("canonical_key")
    @classmethod
    def _check_canonical_key(cls, value: str) -> str:
        if not CANONICAL_KEY_RE.fullmatch(value):
            raise ValueError("canonical_key is not a well-formed catalog key")
        return value

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not SLUG_RE.fullmatch(value):
            raise ValueError("slug is not a well-formed catalog slug")
        return value


class McpEntry(CatalogRecord):
    """One indexed MCP server."""

    kind: EntryKind = "mcp"
    connector_type: str | None = None
    mcp_url: str | None = None
    url_unverified: bool = True
    transport: TransportHint = "unknown"
    auth_hint: AuthHint = "bearer"
    auth_note: str = ""
    setup_note: str = ""
    stdio_only: bool = False
    connector_config: dict[str, Any] = Field(default_factory=dict)
    packages: tuple[PackageRef, ...] = ()
    remotes: tuple[RemoteRef, ...] = ()
    tool_count: int | None = None
    registry_name: str = ""
    smithery_qualified_name: str = ""
    npm_package: str = ""
    verified_upstream: bool = False
    popularity_signals: PopularitySignals = PopularitySignals()


class SkillEntry(CatalogRecord):
    """One indexed agent skill."""

    kind: EntryKind = "skill"
    skill_name: str = ""
    source_ref: str = ""
    skill_path: str = ""
    plugin: PluginRef | None = None
    commit_sha: str = ""
    model_invocable: bool = True
    allowed_tools: tuple[str, ...] = ()
    skill_version: str = ""
    frontmatter_bytes: int | None = None


class LockedSource(_Wire):
    """One upstream registry as the release build saw it."""

    source_id: str = ""
    url: str = ""
    fetched_at: str = ""
    sha256: str = ""
    entry_count: int = 0
    page_count: int = 0


class SourcesLock(_Wire):
    """``sources.lock``: which registries were crawled, when, and how much
    each contributed. Provenance, kept for the operator, not the agent.

    An empty source list parses. A release that indexed nothing is a release
    worth recording, and refusing it would turn a quiet upstream day into a
    failed refresh.
    """

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    sources: tuple[LockedSource, ...] = ()


__all__ = [
    "CANONICAL_KEY_RE",
    "SLUG_RE",
    "SUPPORTED_SCHEMA_VERSION",
    "AuthHint",
    "CatalogFetchError",
    "CatalogFormatError",
    "CatalogIntegrityError",
    "CatalogRecord",
    "CatalogSyncError",
    "EntryKind",
    "EntrySource",
    "LockedSource",
    "McpEntry",
    "PackageRef",
    "PluginRef",
    "PopularitySignals",
    "RemoteRef",
    "RepoRef",
    "SkillEntry",
    "SourcesLock",
    "TransportHint",
    "TrustTier",
    "shard_for",
]
