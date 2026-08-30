"""Routes for the app/skill catalog (docs/architecture/catalog.md).

Two routers, because the catalog is two different things.

``/api/v1/catalog`` is reference data — the same index for everybody on this
install, exactly like ``/api/v1/connectors``. It is not workspace-scoped, takes
any authenticated session, declares no role and no scope, and is read-only.
Nothing on it dials an endpoint, installs anything, or writes a row: it is a
directory somebody browses, and the only text it returns was redacted and
length-capped before it reached the database.

``/api/v1/workspaces/{workspace_id}/catalog`` holds the single write, and it is
a narrow one: applying an entry's trust-tier risk floor to a connection that
workspace already has. Admin, CSRF-protected, ``apps:write``, and one
directional — it can raise a tool's risk, never lower one an admin set by hand.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from jhin_api.catalog import icons, service
from jhin_api.catalog.schemas import (
    CatalogEntryDetailOut,
    CatalogFacetsOut,
    CatalogSearchOut,
    CatalogVersionOut,
    RiskFloorAppliedOut,
    RiskFloorApply,
)
from jhin_api.deps import AdminCtx, CurrentAuth, DbSession
from jhin_api.deps import client_ip_hash as ip_hash
from jhin_api.deps import get_request_id as req_id
from jhin_api.security.csrf import csrf_protect

#: The filter vocabularies, spelled once. Declaring them as literals rather
#: than plain strings means an unknown value is a 422 from FastAPI before any
#: query is built, and the accepted set is published in the OpenAPI document.
KindQuery = Annotated[Literal["mcp", "skill"] | None, Query()]
TrustTierQuery = Annotated[
    Literal["curated", "registry_verified", "smithery_verified", "reviewed", "indexed"] | None,
    Query(),
]
TransportQuery = Annotated[Literal["streamable_http", "sse", "unknown"] | None, Query()]
AuthHintQuery = Annotated[Literal["none", "bearer", "header", "oauth"] | None, Query()]
QueryText = Annotated[str | None, Query(max_length=120)]
CategoryQuery = Annotated[str | None, Query(max_length=64)]
ConnectableQuery = Annotated[bool | None, Query()]
IncludeIndexedQuery = Annotated[bool, Query()]

catalog_router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/catalog",
    tags=["catalog"],
    dependencies=[Depends(csrf_protect)],
)


@catalog_router.get("/entries")
async def list_catalog_entries(
    _auth: CurrentAuth,
    db: DbSession,
    q: QueryText = None,
    kind: KindQuery = None,
    category: CategoryQuery = None,
    trust_tier: TrustTierQuery = None,
    transport: TransportQuery = None,
    auth_hint: AuthHintQuery = None,
    connectable: ConnectableQuery = None,
    include_indexed: IncludeIndexedQuery = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CatalogSearchOut:
    """Search the library: the curated built-ins merged with the active synced
    generation.

    Curated entries always come first — not because they scored higher, but
    because a reviewed integration is what somebody should be offered first.
    Crawled entries (``trust_tier`` ``indexed``) stay hidden unless
    ``include_indexed`` asks for them. ``total`` counts both halves, so paging
    with ``limit`` and ``offset`` walks the merged list once.
    """
    items, total, version = await service.search_entries(
        db,
        q=q,
        kind=kind,
        category=category,
        trust_tier=trust_tier,
        transport=transport,
        auth_hint=auth_hint,
        connectable=connectable,
        include_indexed=include_indexed,
        limit=limit,
        offset=offset,
    )
    return CatalogSearchOut(items=items, total=total, version=version)


@catalog_router.get("/facets")
async def catalog_facets(
    _auth: CurrentAuth,
    db: DbSession,
    q: QueryText = None,
    kind: KindQuery = None,
    category: CategoryQuery = None,
    trust_tier: TrustTierQuery = None,
    transport: TransportQuery = None,
    auth_hint: AuthHintQuery = None,
    connectable: ConnectableQuery = None,
    include_indexed: IncludeIndexedQuery = False,
) -> CatalogFacetsOut:
    """How many entries each filter value would leave, under the filters
    already applied.

    Takes the same parameters as ``/entries`` minus the page window. Each
    dimension is counted with its own selection released, so the chips beside
    the one you picked still say what picking them instead would give.
    """
    return await service.facets(
        db,
        q=q,
        kind=kind,
        category=category,
        trust_tier=trust_tier,
        transport=transport,
        auth_hint=auth_hint,
        connectable=connectable,
        include_indexed=include_indexed,
    )


@catalog_router.get("/entries/{slug}")
async def get_catalog_entry(
    slug: Annotated[str, Path(min_length=1, max_length=32)],
    _auth: CurrentAuth,
    db: DbSession,
) -> CatalogEntryDetailOut:
    """One entry in full, including the Connect dialog's render contract.

    A reserved slug always resolves to the curated entry, whatever a synced row
    claims. The ``config_schema`` field list is built here from the manifest of
    a connector this install actually has — catalog data only ever supplies
    values for names that manifest already declared, so nothing published
    upstream can add a field, rename one, or mark one secret.
    """
    return await service.get_entry(db, slug)


@catalog_router.get(
    "/entries/{slug}/icon",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}, "description": "The entry's logo bytes."}},
)
async def get_catalog_entry_icon(
    slug: Annotated[str, Path(min_length=1, max_length=32)],
    _auth: CurrentAuth,
    db: DbSession,
) -> Response:
    """One entry's logo, served same-origin.

    This is the only place the stored upstream icon URL is ever acted on: the
    proxy re-validates it against the sync's own allowlist, follows at most a
    couple of equally-validated redirects, keeps only bytes whose magic
    numbers name a raster image, and caches the result — success or failure —
    in ``catalog_icon``. 404 means "no logo"; the library card falls back to
    its glyph tile.
    """
    return await icons.serve_icon(db, slug)


@catalog_router.get("/version")
async def get_catalog_version(_auth: CurrentAuth, db: DbSession) -> CatalogVersionOut | None:
    """Which generation of the index is being served, or ``null`` before the
    first sync has run. Shown in the library footer so a stale index is visible
    rather than silently old."""
    return await service.active_version(db)


@router.post("/apply-risk-floor")
async def apply_catalog_risk_floor(
    payload: RiskFloorApply,
    request: Request,
    ctx: AdminCtx,
    db: DbSession,
) -> RiskFloorAppliedOut:
    """Raise a connection's discovered tools to the risk floor its catalog
    entry implies (admin).

    The floor is never in the request: it is resolved from the entry's own
    trust tier, so this can be applied but not chosen. Idempotent and
    one-directional — a tool already at or above the floor is left exactly as
    it is, and nothing an admin raised by hand is ever undone.
    """
    return await service.apply_risk_floor(
        db,
        ctx,
        payload,
        request_id=req_id(request),
        ip_hash=ip_hash(request),
    )
