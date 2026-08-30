"""Response and request shapes for the catalog surface (docs/architecture/catalog.md).

Everything a catalog route returns is public reference data: no workspace
content, no credentials, no per-tenant state. The one write, ``RiskFloorApply``,
names a connection and a catalog slug and nothing else — the risk it applies is
resolved server-side from the entry's own trust tier, never taken from the
request.

Every string here was redacted, stripped of control characters, and capped at
ingest, and is capped again by its column. The models below re-state the
contract the web app renders against; ``ConfigSchemaOut`` in particular is the
render contract for the Connect dialog, and its field *definitions* are always
built from installed connector manifests (see :mod:`.config_schema`) rather
than from anything the catalog supplied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CatalogKind = Literal["mcp", "skill"]
CatalogTier = Literal["curated", "registry_verified", "smithery_verified", "reviewed", "indexed"]
CatalogSource = Literal["builtin", "synced"]
RiskName = Literal["read", "write", "elevated", "destructive"]
TransportHint = Literal["streamable_http", "sse", "unknown"]
AuthHintName = Literal["none", "bearer", "header", "oauth"]


# --- The Connect dialog's render contract (§4.7 of the build spec) ---------


class ConfigSchemaField(BaseModel):
    """One control the Connect dialog should render.

    Field definitions come from the installed connector manifest; the catalog
    only ever supplies a *value* for a name the manifest already declared.
    A renderer that meets a ``type`` it does not know falls back to a text
    input rather than refusing the form.
    """

    name: str
    label: str
    type: Literal["string", "integer", "boolean", "string_list"]
    required: bool = False
    secret: bool = False
    default: str | int | bool | list[str] | None = None
    enum: list[str] = []
    max_length: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    placeholder: str = ""
    help: str = ""
    multiline: bool = False


class ConfigSchemaAuth(BaseModel):
    """How the provider expects to be authenticated, in the entry's own words."""

    type: AuthHintName
    note: str = ""


class ConfigSchemaOut(BaseModel):
    """The whole render contract for one entry, or nothing when no installed
    connector can reach it. ``degraded`` names the fields the server could not
    describe faithfully, so the dialog can say so instead of guessing."""

    version: Literal[1] = 1
    connector_type: str
    fields: list[ConfigSchemaField] = []
    auth: ConfigSchemaAuth
    degraded: list[str] = []


# --- Search hits ----------------------------------------------------------


class CatalogEntryOut(BaseModel):
    """One search hit. Every string here is bounded and already sanitised."""

    slug: str
    kind: CatalogKind
    source: CatalogSource
    name: str
    summary: str
    category: str
    icon: str
    # The same-origin proxy path for the entry's logo, or null when it has
    # none. Never the upstream URL itself: the browser asks this server, and
    # the icon proxy decides whether and what to dial.
    logo_url: str | None = None
    trust_tier: CatalogTier
    default_risk: RiskName
    popularity: float
    connector_type: str | None = None
    mcp_url: str | None = None
    url_unverified: bool = True
    transport: TransportHint = "unknown"
    auth_hint: AuthHintName = "bearer"
    stdio_only: bool = False
    deprecated: bool = False
    connectable: bool
    docs_url: str = ""


class CatalogVersionOut(BaseModel):
    """Which generation of the index answered this request.

    Shown in the gallery footer so a stale index is visible rather than
    silently old. ``null`` on a fresh install: the built-in entries are all
    there is until the first sync runs.
    """

    release_tag: str
    source_repo: str
    data_sha256: str
    entry_count: int
    mcp_count: int
    skill_count: int
    activated_at: datetime | None = None


class CatalogSearchOut(BaseModel):
    items: list[CatalogEntryOut]
    total: int
    version: CatalogVersionOut | None = None


class CatalogFacetBucket(BaseModel):
    value: str
    label: str
    count: int


class CatalogFacetsOut(BaseModel):
    """Counts per dimension under the current filters, each dimension counted
    with its own filter lifted — so a chosen category still shows the other
    categories a person could switch to."""

    kind: list[CatalogFacetBucket] = []
    category: list[CatalogFacetBucket] = []
    trust_tier: list[CatalogFacetBucket] = []
    transport: list[CatalogFacetBucket] = []
    auth_hint: list[CatalogFacetBucket] = []
    total: int


# --- Detail ---------------------------------------------------------------


class CatalogSourceOut(BaseModel):
    """One upstream registry this entry was seen in. Provenance, for a person."""

    source_id: str
    upstream_id: str
    url: str


class CatalogSkillDetailOut(BaseModel):
    skill_name: str = ""
    source_ref: str = ""
    skill_path: str = ""
    commit_sha: str = ""
    marketplace: str = ""
    plugin: str = ""
    model_invocable: bool = True
    allowed_tools: list[str] = []


class CatalogMcpDetailOut(BaseModel):
    tool_count: int | None = None
    registry_name: str = ""
    npm_package: str = ""
    verified_upstream: bool = False
    package_identifiers: list[str] = []
    remote_urls: list[str] = []


class CatalogEntryDetailOut(CatalogEntryOut):
    """Everything the detail dialog shows. Still read-only: nothing here dials
    ``mcp_url``, and ``config_schema`` only pre-fills a form the person submits."""

    description: str = ""
    homepage: str = ""
    auth_note: str = ""
    setup_note: str = ""
    license: str = ""
    tags: list[str] = []
    connector_config: dict[str, str] = {}
    sources: list[CatalogSourceOut] = []
    config_schema: ConfigSchemaOut | None = None
    mcp: CatalogMcpDetailOut | None = None
    skill: CatalogSkillDetailOut | None = None


# --- The one write --------------------------------------------------------


class RiskFloorApply(BaseModel):
    """Raise a connection's discovered tools to the floor its catalog entry
    implies. The floor itself is never in the request: it is resolved from the
    entry's trust tier, so this can only ever be applied, never chosen."""

    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    slug: str = Field(min_length=1, max_length=32)


class RiskFloorAppliedOut(BaseModel):
    connection_id: UUID
    floor: RiskName
    tools_raised: int
    tools_unchanged: int
