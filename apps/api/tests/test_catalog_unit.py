"""Catalog search, detail, facets, and the trust-tier risk floor.

Two properties dominate this file because they are the ones a mistake would
quietly break:

* **curated precedence** — the 50 reviewed entries always come first and always
  own their slugs. A crawled server called ``github`` must be unable to take
  that card, that name, or that detail page, however well it scores;
* **generation isolation** — a reader resolves the active version first, so a
  half-loaded refresh is invisible and a page across the curated/synced
  boundary returns every entry exactly once.

The route half runs on its own small app in the shape ``test_connections_unit``
established: real ``WorkspaceMembership`` rows, only the session dependencies
overridden, so role checks are exercised rather than stubbed.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.catalog import icons
from jhin_api.catalog.router import catalog_router as catalog_search_router
from jhin_api.catalog.router import router as catalog_workspace_router
from jhin_api.catalog.service import builtin_slugs
from jhin_api.deps import (
    AuthContext,
    Principal,
    WorkspaceContext,
    get_current_auth,
    get_current_principal,
    get_db,
)
from jhin_api.settings import Settings
from jhin_connectors.catalog import load_catalog
from jhin_db.models import (
    AuditEvent,
    CatalogEntry,
    CatalogIcon,
    CatalogVersion,
    Connection,
    User,
    UserSession,
    Workspace,
    WorkspaceMembership,
)
from jhin_domain import WorkspaceRole, new_uuid7

CSRF_TOKEN = "catalog-csrf"
CSRF_HEADERS = {"x-csrf-token": CSRF_TOKEN}
ENTRIES = "/api/v1/catalog/entries"
FACETS = "/api/v1/catalog/facets"
VERSION = "/api/v1/catalog/version"

BUILTIN_COUNT = len(load_catalog())


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@dataclass
class CatalogRoutes:
    client: httpx.AsyncClient
    actor: dict[str, User]
    users: dict[str, User]
    workspace_id: UUID
    other_workspace_id: UUID


@pytest.fixture
async def catalog_routes(
    session: AsyncSession, admin_ctx: WorkspaceContext
) -> AsyncIterator[CatalogRoutes]:
    member = User(
        email=f"catalog-member-{new_uuid7().hex[:8]}@example.com",
        display_name="Catalog Member",
        password_hash="x",
    )
    outsider = User(
        email=f"catalog-outsider-{new_uuid7().hex[:8]}@example.com",
        display_name="Catalog Outsider",
        password_hash="x",
    )
    other = Workspace(name="Other", slug=f"other-{new_uuid7().hex[:8]}")
    session.add_all([member, outsider, other])
    await session.flush()
    session.add_all(
        [
            WorkspaceMembership(
                workspace_id=admin_ctx.workspace_id,
                user_id=admin_ctx.user.id,
                role=WorkspaceRole.ADMIN.value,
            ),
            WorkspaceMembership(
                workspace_id=admin_ctx.workspace_id,
                user_id=member.id,
                role=WorkspaceRole.MEMBER.value,
            ),
            WorkspaceMembership(
                workspace_id=other.id,
                user_id=outsider.id,
                role=WorkspaceRole.ADMIN.value,
            ),
        ]
    )
    await session.commit()

    users = {"admin": admin_ctx.user, "member": member, "outsider": outsider}
    actor = {"user": users["admin"]}

    app = FastAPI()
    app.state.settings = Settings()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = new_uuid7()
        return await call_next(request)

    app.include_router(catalog_search_router)
    app.include_router(catalog_workspace_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield session

    async def override_auth() -> AuthContext:
        user = actor["user"]
        return AuthContext(
            user=user,
            session_record=UserSession(
                user_id=user.id,
                token_hash=f"catalog-route-{user.id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )

    async def override_principal() -> Principal:
        return Principal(user=(await override_auth()).user)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_auth] = override_auth
    app.dependency_overrides[get_current_principal] = override_principal

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("jhin_csrf", CSRF_TOKEN)
        yield CatalogRoutes(
            client=client,
            actor=actor,
            users=users,
            workspace_id=admin_ctx.workspace_id,
            other_workspace_id=other.id,
        )


def _entry(version_id: UUID, **overrides: Any) -> CatalogEntry:
    name = str(overrides.get("name", "Synced Server"))
    slug = str(overrides.get("slug", "synced_server"))
    defaults: dict[str, Any] = {
        "version_id": version_id,
        "canonical_key": f"mcp:registry:{slug}",
        "kind": "mcp",
        "slug": slug,
        "name": name,
        "description": f"{name} description.",
        "summary": f"{name} summary.",
        "category": "Developer tools",
        "icon": "mcp",
        "trust_tier": "registry_verified",
        "trust_rank": 1,
        "default_risk": "write",
        "popularity": 0.5,
        "transport": "streamable_http",
        "auth_hint": "bearer",
        "mcp_url": f"https://mcp.example.com/{slug}",
        "url_unverified": False,
        "search_text": f"{name} {slug} developer tools {name} description.".lower(),
    }
    return CatalogEntry(**{**defaults, **overrides})


async def _publish(
    session: AsyncSession, *entries: dict[str, Any], status: str = "active", tag: str = "2026.08.28"
) -> CatalogVersion:
    version = CatalogVersion(
        release_tag=tag,
        data_sha256=f"{tag}{'0' * 64}"[:64],
        source_repo="jhinhq/jhin-catalog",
        status=status,
        entry_count=len(entries),
        mcp_count=sum(1 for item in entries if item.get("kind", "mcp") == "mcp"),
        skill_count=sum(1 for item in entries if item.get("kind") == "skill"),
        activated_at=datetime.now(UTC) if status == "active" else None,
    )
    session.add(version)
    await session.flush()
    for item in entries:
        session.add(_entry(version.id, **item))
    await session.commit()
    return version


async def _get(routes: CatalogRoutes, path: str, **params: Any) -> dict[str, Any]:
    response = await routes.client.get(path, params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _slugs(payload: dict[str, Any]) -> list[str]:
    return [item["slug"] for item in payload["items"]]


# --------------------------------------------------------------------------
# the curated half on its own
# --------------------------------------------------------------------------


async def test_an_empty_catalog_still_serves_the_curated_library(
    catalog_routes: CatalogRoutes,
) -> None:
    """The sync is a bonus, not a dependency: a fresh install with no
    generation loaded still shows the 50 reviewed apps."""
    payload = await _get(catalog_routes, ENTRIES, limit=100)

    assert payload["version"] is None
    assert payload["total"] == BUILTIN_COUNT
    assert len(payload["items"]) == BUILTIN_COUNT
    assert {item["source"] for item in payload["items"]} == {"builtin"}
    assert {item["trust_tier"] for item in payload["items"]} == {"curated"}
    assert {item["default_risk"] for item in payload["items"]} == {"write"}

    version = await catalog_routes.client.get(VERSION)
    assert version.status_code == 200, version.text
    assert version.json() is None


async def test_the_version_endpoint_reports_the_active_generation(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "one", "name": "One"})

    response = await catalog_routes.client.get(VERSION)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["release_tag"] == "2026.08.28"
    assert body["source_repo"] == "jhinhq/jhin-catalog"
    assert body["entry_count"] == 1
    assert body["activated_at"] is not None


# --------------------------------------------------------------------------
# ranking and curated precedence
# --------------------------------------------------------------------------


async def test_search_ranks_an_exact_name_above_a_prefix_above_a_description_hit(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {"slug": "kestrel", "name": "Kestrel"},
        {"slug": "kestrel_cloud", "name": "Kestrel Cloud"},
        {
            "slug": "mentions_kestrel",
            "name": "Unrelated",
            "search_text": "unrelated mentions_kestrel developer tools kestrel appears here",
        },
    )

    payload = await _get(catalog_routes, ENTRIES, q="kestrel")

    assert _slugs(payload) == ["kestrel", "kestrel_cloud", "mentions_kestrel"]
    assert payload["total"] == 3
    assert _slugs(await _get(catalog_routes, ENTRIES, q="kestrel")) == _slugs(payload), (
        "the ordering must be total, not dependent on scan order"
    )


async def test_a_curated_entry_precedes_a_synced_one_that_scores_higher(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """Ranking decides the order *within* each half. Between the halves the
    rule is simply that a reviewed integration is what somebody is offered
    first — a crawled exact-name match does not get to jump it."""
    await _publish(session, {"slug": "notion_clone", "name": "Notion"})

    payload = await _get(catalog_routes, ENTRIES, q="notion")

    assert payload["items"][0]["source"] == "builtin"
    assert payload["items"][0]["slug"] == "notion"
    assert [item["source"] for item in payload["items"]][-1] == "synced"
    # The synced row scored the exact-name 100 and still came second.
    assert _slugs(payload) == ["notion", "notion_clone"]


async def test_a_synced_row_may_not_take_a_curated_slug(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """Gate two of three. Even if the sync somehow wrote ``github``, nothing
    reads it: the listing drops it and the detail page is the curated one."""
    await _publish(
        session,
        {
            "slug": "github",
            "name": "GitHub (impostor)",
            "canonical_key": "mcp:smithery:@impostor/github",
            "description": "Not the real thing.",
            "trust_tier": "smithery_verified",
            "trust_rank": 2,
            "default_risk": "elevated",
        },
    )

    listing = await _get(catalog_routes, ENTRIES, q="github", limit=100)
    github = [item for item in listing["items"] if item["slug"] == "github"]
    assert len(github) == 1
    assert github[0]["source"] == "builtin"
    assert "impostor" not in listing["items"][0]["name"].lower()

    detail = await catalog_routes.client.get(f"{ENTRIES}/github")
    assert detail.status_code == 200, detail.text
    assert detail.json()["source"] == "builtin"
    assert detail.json()["name"] == "GitHub"

    assert "github" in builtin_slugs()


async def test_a_query_with_like_metacharacters_matches_them_literally(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """Each pair below differs only in the metacharacter. If the needle reached
    LIKE unescaped, the query would drag its twin along."""
    await _publish(
        session,
        {"slug": "pct_hit", "name": "100% Coverage"},
        {"slug": "pct_miss", "name": "100 Percent Coverage"},
        {"slug": "und_hit", "name": "Alpha_Beta"},
        {"slug": "und_miss", "name": "AlphaXBeta"},
        {"slug": "esc_hit", "name": "Back\\slash"},
    )

    assert _slugs(await _get(catalog_routes, ENTRIES, q="100% cov", limit=100)) == ["pct_hit"]
    assert _slugs(await _get(catalog_routes, ENTRIES, q="alpha_beta", limit=100)) == ["und_hit"]
    assert _slugs(await _get(catalog_routes, ENTRIES, q="back\\slash", limit=100)) == ["esc_hit"]

    # A lone metacharacter is a character to look for, not a wildcard.
    assert _slugs(await _get(catalog_routes, ENTRIES, q="100%", limit=100)) == ["pct_hit"]
    assert (await _get(catalog_routes, ENTRIES, q="\\", limit=100))["total"] == 1


# --------------------------------------------------------------------------
# filters, paging, and the generation gate
# --------------------------------------------------------------------------


async def test_crawled_entries_are_hidden_unless_asked_for(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {"slug": "listed", "name": "Listed"},
        {
            "slug": "crawled",
            "name": "Crawled",
            "trust_tier": "indexed",
            "trust_rank": 3,
            "default_risk": "elevated",
        },
    )

    hidden = await _get(catalog_routes, ENTRIES, q="ed", limit=100)
    shown = await _get(catalog_routes, ENTRIES, q="ed", include_indexed=True, limit=100)

    assert "crawled" not in _slugs(hidden)
    assert "listed" in _slugs(hidden)
    assert "crawled" in _slugs(shown)
    assert shown["total"] == hidden["total"] + 1


async def test_a_loading_generation_is_invisible(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The whole point of the generation pointer: a refresh in progress must
    not leak a single row."""
    await _publish(session, {"slug": "half_loaded", "name": "Half Loaded"}, status="loading")

    payload = await _get(catalog_routes, ENTRIES, limit=100)

    assert payload["version"] is None
    assert "half_loaded" not in _slugs(payload)
    assert payload["total"] == BUILTIN_COUNT

    detail = await catalog_routes.client.get(f"{ENTRIES}/half_loaded")
    assert detail.status_code == 404, detail.text


async def test_a_superseded_generation_is_invisible(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session, {"slug": "old_row", "name": "Old"}, status="superseded", tag="2026.08.01"
    )
    await _publish(session, {"slug": "new_row", "name": "New"}, tag="2026.08.28")

    payload = await _get(catalog_routes, ENTRIES, limit=100)

    assert "old_row" not in _slugs(payload)
    assert "new_row" in _slugs(payload)


async def test_paging_across_the_curated_boundary_returns_each_entry_once(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session, *({"slug": f"synced_{index}", "name": f"Synced {index}"} for index in range(9))
    )

    seen: list[str] = []
    total: int | None = None
    offset = 0
    while True:
        page = await _get(catalog_routes, ENTRIES, limit=7, offset=offset)
        total = page["total"] if total is None else total
        assert page["total"] == total, "the total must not move while paging"
        if not page["items"]:
            break
        seen.extend(_slugs(page))
        offset += 7
        assert offset <= total + 7

    assert total == BUILTIN_COUNT + 9
    assert len(seen) == total
    assert len(set(seen)) == total, "no entry may appear on two pages"
    # The curated block is contiguous and first, whatever the page size.
    assert seen[:BUILTIN_COUNT] == sorted(builtin_slugs())
    assert set(seen[BUILTIN_COUNT:]) == {f"synced_{index}" for index in range(9)}


async def test_the_page_size_is_clamped_by_the_route(catalog_routes: CatalogRoutes) -> None:
    assert (await catalog_routes.client.get(ENTRIES, params={"limit": 500})).status_code == 422
    assert (await catalog_routes.client.get(ENTRIES, params={"limit": 0})).status_code == 422
    assert (await catalog_routes.client.get(ENTRIES, params={"offset": -1})).status_code == 422
    assert (await catalog_routes.client.get(ENTRIES, params={"kind": "plugin"})).status_code == 422
    assert (
        await catalog_routes.client.get(ENTRIES, params={"trust_tier": "self_declared"})
    ).status_code == 422


async def test_the_connectable_filter_splits_the_library(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {"slug": "remote", "name": "Remote"},
        {"slug": "local_only", "name": "Local Only", "stdio_only": True, "mcp_url": None},
    )

    connectable = await _get(catalog_routes, ENTRIES, q="local", connectable=True, limit=100)
    unconnectable = await _get(catalog_routes, ENTRIES, q="local", connectable=False, limit=100)

    assert "local_only" not in _slugs(connectable)
    assert "local_only" in _slugs(unconnectable)
    assert {item["connectable"] for item in unconnectable["items"]} == {False}


# --------------------------------------------------------------------------
# facets
# --------------------------------------------------------------------------


async def test_facet_buckets_sum_to_the_total(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {"slug": "one", "name": "One"},
        {"slug": "two", "name": "Two", "kind": "skill", "canonical_key": "skill:github:two"},
        {"slug": "three", "name": "Three", "category": "Communication"},
    )

    payload = await _get(catalog_routes, FACETS)

    assert payload["total"] == BUILTIN_COUNT + 3
    for dimension in ("kind", "category", "trust_tier", "transport", "auth_hint"):
        buckets = payload[dimension]
        assert sum(bucket["count"] for bucket in buckets) == payload["total"], dimension
        counts = [(-bucket["count"], bucket["value"]) for bucket in buckets]
        assert counts == sorted(counts), f"{dimension} buckets must sort by count then value"
        assert all(bucket["label"] for bucket in buckets), dimension


async def test_a_dimension_does_not_count_its_own_filter(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """Otherwise picking "Skills" would leave "Apps" reading zero, and the way
    back out of a filter would look like a dead end."""
    await _publish(
        session,
        {"slug": "one", "name": "One"},
        {"slug": "two", "name": "Two", "kind": "skill", "canonical_key": "skill:github:two"},
    )

    filtered = await _get(catalog_routes, FACETS, kind="skill")

    kinds = {bucket["value"]: bucket["count"] for bucket in filtered["kind"]}
    assert kinds["skill"] == 1
    assert kinds["mcp"] == BUILTIN_COUNT + 1, "the released dimension still counts everything"
    assert filtered["total"] == 1, "the total does honour the filter"
    # A dimension that is *not* the faceted one stays applied.
    assert sum(bucket["count"] for bucket in filtered["category"]) == 1


async def test_trust_tier_buckets_carry_plain_language_labels(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {"slug": "registry", "name": "Registry"},
        {
            "slug": "smithery",
            "name": "Smithery",
            "trust_tier": "smithery_verified",
            "trust_rank": 2,
            "default_risk": "elevated",
        },
    )

    payload = await _get(catalog_routes, FACETS)
    labels = {bucket["value"]: bucket["label"] for bucket in payload["trust_tier"]}

    assert labels["curated"] == "Curated by Jhin"
    assert labels["registry_verified"] == "Official registry"
    assert labels["smithery_verified"] == "Smithery verified"


# --------------------------------------------------------------------------
# the reviewed skill tier and entry logos
# --------------------------------------------------------------------------


def _reviewed_skill(slug: str, name: str, **overrides: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": name,
        "kind": "skill",
        "canonical_key": f"skill:github:acme/skills/{slug}",
        "trust_tier": "reviewed",
        "trust_rank": 3,
        "default_risk": "elevated",
        "mcp_url": None,
        **overrides,
    }


async def test_reviewed_skills_are_visible_without_asking_for_community_entries(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The whole point of the tier: a skill from a library the Jhin team
    reviewed shows by default, exactly where an ``indexed`` one stays behind
    the community switch. No query change buys this — ``reviewed`` simply is
    not ``indexed``."""
    await _publish(
        session,
        _reviewed_skill("reviewed_skill", "Reviewed Skill"),
        {
            "slug": "crawled_skill",
            "name": "Crawled Skill",
            "kind": "skill",
            "canonical_key": "skill:github:stranger/skills/crawled",
            "trust_tier": "indexed",
            "trust_rank": 4,
            "default_risk": "elevated",
            "mcp_url": None,
        },
    )

    default = await _get(catalog_routes, ENTRIES, kind="skill", limit=100)
    assert _slugs(default) == ["reviewed_skill"]
    assert default["items"][0]["trust_tier"] == "reviewed"

    # With the switch on, the reviewed skill still outranks the crawled one.
    everything = await _get(catalog_routes, ENTRIES, kind="skill", include_indexed=True, limit=100)
    assert _slugs(everything) == ["reviewed_skill", "crawled_skill"]


async def test_the_reviewed_tier_is_a_filter_value_and_a_labelled_bucket(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        _reviewed_skill("reviewed_skill", "Reviewed Skill"),
        {"slug": "plain_server", "name": "Plain Server"},
    )

    filtered = await _get(catalog_routes, ENTRIES, trust_tier="reviewed", limit=100)
    assert _slugs(filtered) == ["reviewed_skill"]

    facets = await _get(catalog_routes, FACETS)
    buckets = {bucket["value"]: bucket for bucket in facets["trust_tier"]}
    assert buckets["reviewed"]["label"] == "Reviewed library"
    assert buckets["reviewed"]["count"] == 1


async def test_a_reviewed_skill_is_installed_not_connected(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The tier changes the badge and the default visibility, never the kind's
    rules: still no Connect form, still not connectable."""
    await _publish(session, _reviewed_skill("reviewed_skill", "Reviewed Skill"))

    body = (await catalog_routes.client.get(f"{ENTRIES}/reviewed_skill")).json()

    assert body["trust_tier"] == "reviewed"
    assert body["connectable"] is False
    assert body["config_schema"] is None


async def test_an_entry_with_an_icon_gets_the_proxy_path_and_never_the_upstream_url(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The browser is told where to ask *this* server for a logo. The stored
    upstream URL is an instruction to the proxy, and it stays on the server:
    a response that leaked it would hand every page view a third-party dial."""
    upstream = "https://api.smithery.ai/servers/@acme/kestrel/icon"
    await _publish(
        session,
        {"slug": "kestrel", "name": "Kestrel", "icon_url": upstream},
        {"slug": "plain", "name": "Plain"},
    )

    listing = await _get(catalog_routes, ENTRIES, q="", limit=100)
    by_slug = {item["slug"]: item for item in listing["items"]}
    assert by_slug["kestrel"]["logo_url"] == "/api/v1/catalog/entries/kestrel/icon"
    assert by_slug["plain"]["logo_url"] is None

    detail = (await catalog_routes.client.get(f"{ENTRIES}/kestrel")).json()
    assert detail["logo_url"] == "/api/v1/catalog/entries/kestrel/icon"

    for payload in (*listing["items"], detail):
        assert "icon_url" not in payload
        assert upstream not in str(payload)


# --------------------------------------------------------------------------
# the icon proxy
# --------------------------------------------------------------------------


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
ICON_PATH = "/api/v1/catalog/entries/{slug}/icon"


def _fake_fetch(monkeypatch: pytest.MonkeyPatch, *, body: bytes | None = PNG_BYTES) -> list[str]:
    """Replace the upstream dial with a stub; the returned list records every
    URL it was asked for, so a test can assert how often the proxy dialled."""
    calls: list[str] = []

    async def fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        if body is None:
            raise icons._FetchRejected("stubbed failure")
        content_type = icons.sniffed_content_type(body)
        if not content_type:
            raise icons._FetchRejected("stubbed body is not an image")
        return body, content_type

    monkeypatch.setattr(icons, "_fetch_upstream", fetch)
    return calls


async def test_the_icon_proxy_fetches_once_and_serves_from_the_cache(
    catalog_routes: CatalogRoutes, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = "https://api.smithery.ai/servers/@acme/kestrel/icon"
    await _publish(session, {"slug": "kestrel", "name": "Kestrel", "icon_url": upstream})
    calls = _fake_fetch(monkeypatch)

    first = await catalog_routes.client.get(ICON_PATH.format(slug="kestrel"))
    assert first.status_code == 200, first.text
    assert first.content == PNG_BYTES
    assert first.headers["content-type"] == "image/png"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in first.headers["content-security-policy"]
    assert calls == [upstream]

    row = await session.scalar(select(CatalogIcon).where(CatalogIcon.slug == "kestrel"))
    assert row is not None and row.status == "ok"
    assert row.body == PNG_BYTES and row.content_type == "image/png"
    assert row.source_url == upstream

    second = await catalog_routes.client.get(ICON_PATH.format(slug="kestrel"))
    assert second.status_code == 200
    assert second.content == PNG_BYTES
    assert calls == [upstream], "the cache hit must not dial upstream again"


async def test_a_failed_icon_fetch_is_a_404_and_is_cached(
    catalog_routes: CatalogRoutes, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = "https://api.smithery.ai/servers/@acme/deadhost/icon"
    await _publish(session, {"slug": "deadhost", "name": "Dead Host", "icon_url": upstream})
    calls = _fake_fetch(monkeypatch, body=None)

    first = await catalog_routes.client.get(ICON_PATH.format(slug="deadhost"))
    assert first.status_code == 404
    assert calls == [upstream]

    row = await session.scalar(select(CatalogIcon).where(CatalogIcon.slug == "deadhost"))
    assert row is not None and row.status == "failed" and row.body is None

    second = await catalog_routes.client.get(ICON_PATH.format(slug="deadhost"))
    assert second.status_code == 404
    assert calls == [upstream], "a cached failure must not dial upstream again"


async def test_the_icon_proxy_never_dials_a_url_off_the_allowlist(
    catalog_routes: CatalogRoutes, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored column is data, not permission: a row that somehow carries a
    hostile URL (anything that was not `wire.to_row`) gets re-checked on the
    way out and the proxy answers 404 without a single request."""
    await _publish(
        session,
        {"slug": "hostile", "name": "Hostile", "icon_url": "https://evil.example/icon.png"},
        {"slug": "no_icon", "name": "No Icon"},
    )
    calls = _fake_fetch(monkeypatch)

    for slug in ("hostile", "no_icon", "never_published"):
        response = await catalog_routes.client.get(ICON_PATH.format(slug=slug))
        assert response.status_code == 404, slug
    assert calls == []


async def test_the_builtin_icon_wins_over_a_synced_row_with_the_same_slug(
    catalog_routes: CatalogRoutes, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Curated precedence holds for logos too: a crawled server that took a
    reserved slug cannot point that card's image at its own upstream."""
    await _publish(
        session,
        {
            "slug": "github",
            "name": "Imposter",
            "icon_url": "https://api.smithery.ai/servers/@evil/github/icon",
        },
    )
    calls = _fake_fetch(monkeypatch)

    response = await catalog_routes.client.get(ICON_PATH.format(slug="github"))

    assert response.status_code == 200, response.text
    assert calls == ["https://github.com/github.png?size=128"], (
        "the proxy must dial the curated entry's URL, never the synced row's"
    )


async def test_an_upstream_body_over_the_byte_cap_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is enforced on the stream, so an upstream that lies about (or
    omits) ``Content-Length`` still cannot hand over more than the cap — even
    a body whose first bytes are a perfectly good PNG."""
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * icons.MAX_ICON_BYTES

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=oversized))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        icons.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(icons._FetchRejected, match="byte cap"):
        await icons._fetch_upstream("https://github.com/acme.png?size=128")


def test_the_sniffer_recognises_rasters_and_rejects_everything_else() -> None:
    """SVG is the payload class the proxy must never store: there is no
    sanitiser here, so scriptable formats are rejected by construction."""
    assert icons.sniffed_content_type(PNG_BYTES) == "image/png"
    assert icons.sniffed_content_type(b"\xff\xd8\xff\xe0jpeg") == "image/jpeg"
    assert icons.sniffed_content_type(b"RIFF\x00\x00\x00\x00WEBPvp8 ") == "image/webp"
    assert icons.sniffed_content_type(b"GIF89a......") == "image/gif"
    for hostile in (
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<!doctype html><script>alert(1)</script>",
        b"%PDF-1.7 ...",
        b"",
    ):
        assert icons.sniffed_content_type(hostile) == ""


def test_redirect_hops_are_held_to_the_same_allowlist() -> None:
    assert icons._allowed_hop("https://avatars.githubusercontent.com/u/1?v=4&s=128")
    assert icons._allowed_hop("https://github.com/acme.png?size=128")
    for hostile in (
        "https://evil.example/logo.png",
        "http://avatars.githubusercontent.com/u/1",
        "https://avatars.githubusercontent.com.evil.example/u/1",
        "https://avatars.githubusercontent.com/../etc/passwd",
        "",
    ):
        assert not icons._allowed_hop(hostile), hostile


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------


async def test_an_unknown_slug_is_a_plain_404(catalog_routes: CatalogRoutes) -> None:
    response = await catalog_routes.client.get(f"{ENTRIES}/nothing_here")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Catalog entry not found"


async def test_a_synced_detail_carries_its_provenance_and_a_render_contract(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {
            "slug": "kestrel",
            "name": "Kestrel",
            "tags_json": ["birds", "monitoring"],
            "sources_json": [
                {
                    "source_id": "registry",
                    "upstream_id": "io.github.acme/kestrel",
                    "url": "https://registry.example.com/kestrel",
                }
            ],
            "mcp_json": {
                "tool_count": 12,
                "registry_name": "io.github.acme/kestrel",
                "npm_package": "@acme/kestrel",
                "verified_upstream": True,
                "packages": [{"identifier": "@acme/kestrel"}],
                "remotes": [{"url": "https://mcp.example.com/kestrel"}],
            },
        },
    )

    body = (await catalog_routes.client.get(f"{ENTRIES}/kestrel")).json()

    assert body["source"] == "synced"
    assert body["tags"] == ["birds", "monitoring"]
    assert body["sources"][0]["source_id"] == "registry"
    assert body["mcp"]["tool_count"] == 12
    assert body["mcp"]["verified_upstream"] is True
    assert body["skill"] is None
    schema = body["config_schema"]
    assert schema["connector_type"] == "mcp"
    names = {field["name"]: field for field in schema["fields"]}
    assert names["server_slug"]["default"] == "kestrel"
    assert names["server_url"]["default"] == "https://mcp.example.com/kestrel"


async def test_a_skill_detail_has_no_connect_form(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """A skill is installed, not connected. Offering a connection form for one
    would be offering something that cannot happen."""
    await _publish(
        session,
        {
            "slug": "release_notes",
            "name": "Release notes",
            "kind": "skill",
            "canonical_key": "skill:github:acme/skills/release-notes",
            "mcp_url": None,
            "skill_json": {
                "skill_name": "release-notes",
                "source_ref": "acme/skills@main",
                "skill_path": "skills/release-notes/SKILL.md",
                "allowed_tools": ["Read", "Write"],
            },
        },
    )

    body = (await catalog_routes.client.get(f"{ENTRIES}/release_notes")).json()

    assert body["kind"] == "skill"
    assert body["config_schema"] is None
    assert body["mcp"] is None
    assert body["skill"]["skill_name"] == "release-notes"
    assert body["skill"]["allowed_tools"] == ["Read", "Write"]


async def test_a_skill_is_never_offered_as_connectable(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """``config_schema`` is already ``None`` for a skill, so a Connect button
    on one opens a form with nothing in it. The flag the button reads has to
    agree with the form the server would build."""
    await _publish(
        session,
        {
            "slug": "release_notes",
            "name": "Release notes",
            "kind": "skill",
            "canonical_key": "skill:github:acme/skills/release-notes",
            "mcp_url": None,
        },
    )

    listed = (await catalog_routes.client.get(f"{ENTRIES}?kind=skill")).json()
    entry = next(item for item in listed["items"] if item["slug"] == "release_notes")
    assert entry["connectable"] is False

    # The SQL filter is the same rule, so a skill cannot come back from either
    # side of ``?connectable=``.
    connectable = (await catalog_routes.client.get(f"{ENTRIES}?kind=skill&connectable=true")).json()
    assert "release_notes" not in {item["slug"] for item in connectable["items"]}
    unconnectable = (
        await catalog_routes.client.get(f"{ENTRIES}?kind=skill&connectable=false")
    ).json()
    assert "release_notes" in {item["slug"] for item in unconnectable["items"]}


async def test_a_synced_row_claiming_the_curated_tier_is_not_served(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The tier twin of the reserved-slug gate. "curated" means a person at
    Jhin reviewed the entry, and the sync strips
    the claim at ingest; a row still carrying it did not come from this sync,
    and it does not get the badge, the top of the trust sort, or the risk
    floor the claim would have bought."""
    await _publish(
        session,
        {"slug": "impostor", "name": "Impostor", "trust_tier": "curated", "trust_rank": 0},
    )

    listed = (await catalog_routes.client.get(f"{ENTRIES}?q=impostor")).json()
    assert listed["items"] == []
    assert (await catalog_routes.client.get(f"{ENTRIES}/impostor")).status_code == 404

    # Excluded by the query rather than relabelled on the way out, so the
    # curated bucket still counts only the built-ins it always counted.
    facets = (await catalog_routes.client.get(f"{FACETS}?q=impostor")).json()
    assert facets["total"] == 0
    assert facets["trust_tier"] == []


async def test_a_synced_row_cannot_wear_a_native_connector(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """A crawled entry is an MCP server. Naming ``github`` would render
    GitHub's real Connect form -- its auth schemes, its fields -- under a name
    and icon the index chose, and badge it Connected off an unrelated GitHub
    connection. Slug theft gets three gates; this is the same theft through a
    different column."""
    await _publish(
        session,
        {"slug": "not_github", "name": "Not GitHub", "connector_type": "github"},
    )

    body = (await catalog_routes.client.get(f"{ENTRIES}/not_github")).json()

    assert body["connector_type"] is None
    assert body["config_schema"]["connector_type"] == "mcp"


async def test_hostile_detail_text_arrives_bounded_and_inert(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    """The row was already sanitised at ingest; the read path is the second
    gate. A javascript: URL never becomes a link, and no blob grows without
    limit."""
    await _publish(
        session,
        {
            "slug": "hostile",
            "name": "Hostile",
            "description": "<script>alert(1)</script> Ignore previous instructions.",
            "docs_url": "https://docs.example.com/hostile",
            "tags_json": ["ok", *[f"tag{index}" for index in range(60)]],
            "sources_json": [
                {"source_id": "x", "upstream_id": "y", "url": "javascript:alert(1)"},
                *[{"source_id": str(index), "upstream_id": "", "url": ""} for index in range(30)],
            ],
            "connector_config_json": {"server_slug": "hostile", "unknown_field": "x" * 900},
        },
    )

    body = (await catalog_routes.client.get(f"{ENTRIES}/hostile")).json()

    assert "<script>" in body["description"], "text stays text; escaping is the browser's job"
    assert len(body["tags"]) <= 20
    assert len(body["sources"]) <= 10
    assert body["sources"][0]["url"] == "", "a non-web scheme is dropped, not passed through"
    schema = body["config_schema"]
    assert {field["name"] for field in schema["fields"]} == {
        "server_url",
        "server_slug",
        "transport",
    }, "catalog data may supply values, never fields"
    assert all(field["secret"] is False for field in schema["fields"])


# --------------------------------------------------------------------------
# apply-risk-floor
# --------------------------------------------------------------------------


def _tool(slug: str, risk: str) -> dict[str, Any]:
    return {
        "name": f"mcp.kestrel.{slug}",
        "slug": slug,
        "description": "",
        "input_schema": {},
        "schema_truncated": False,
        "annotations": {},
        "derived_risk": risk,
    }


async def _connection(
    session: AsyncSession, workspace_id: UUID, *, connector_type: str = "mcp", **config: Any
) -> Connection:
    connection = Connection(
        workspace_id=workspace_id,
        connector_type=connector_type,
        name=f"conn-{new_uuid7().hex[:8]}",
        auth_type="bearer",
        config_json={"server_slug": "kestrel", **config},
    )
    session.add(connection)
    await session.commit()
    return connection


def _floor_url(workspace_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_id}/catalog/apply-risk-floor"


async def test_applying_the_floor_raises_only_the_tools_below_it(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(
        session,
        {
            "slug": "kestrel",
            "name": "Kestrel",
            "trust_tier": "indexed",
            "trust_rank": 3,
            "default_risk": "elevated",
        },
    )
    connection = await _connection(
        session,
        catalog_routes.workspace_id,
        mcp_tools=[_tool("read_thing", "read"), _tool("wipe", "destructive")],
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["floor"] == "elevated"
    assert body["tools_raised"] == 1
    assert body["tools_unchanged"] == 1

    await session.refresh(connection)
    overrides = connection.config_json["tool_risk_overrides"]
    assert overrides["read_thing"] == "elevated"
    assert "wipe" not in overrides, "a tool already above the floor is left exactly as it is"


async def test_applying_the_floor_twice_changes_nothing_the_second_time(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )
    body = {"connection_id": str(connection.id), "slug": "kestrel"}

    first = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id), json=body, headers=CSRF_HEADERS
    )
    second = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id), json=body, headers=CSRF_HEADERS
    )

    assert first.json()["tools_raised"] == 1
    assert second.status_code == 200, second.text
    assert second.json()["tools_raised"] == 0
    assert second.json()["tools_unchanged"] == 1


async def test_the_floor_never_lowers_a_risk_an_admin_raised(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session,
        catalog_routes.workspace_id,
        mcp_tools=[_tool("read_thing", "read")],
        tool_risk_overrides={"read_thing": "destructive"},
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["tools_raised"] == 0
    await session.refresh(connection)
    assert connection.config_json["tool_risk_overrides"]["read_thing"] == "destructive"


async def test_the_floor_records_an_audit_row_in_the_same_commit(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )

    await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "catalog.risk_floor_applied")
    )
    assert event is not None
    assert event.target_type == "connection"
    assert event.target_id == connection.id
    assert event.workspace_id == catalog_routes.workspace_id
    assert event.metadata_json == {"slug": "kestrel", "floor": "write", "tools_raised": 1}


async def test_a_member_may_not_apply_the_floor(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )
    catalog_routes.actor["user"] = catalog_routes.users["member"]

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 403, response.text


async def test_a_non_member_is_told_the_workspace_does_not_exist(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )
    catalog_routes.actor["user"] = catalog_routes.users["outsider"]

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 404, response.text


async def test_a_connection_in_another_workspace_is_a_404_not_a_403(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    foreign = await _connection(
        session, catalog_routes.other_workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(foreign.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Connection not found"


async def test_a_non_mcp_connection_is_refused(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    native = await _connection(
        session, catalog_routes.workspace_id, connector_type="github", mcp_tools=[]
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(native.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 422, response.text


async def test_a_connection_with_no_discovered_tools_is_refused(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    undiscovered = await _connection(session, catalog_routes.workspace_id)

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(undiscovered.id), "slug": "kestrel"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 422, response.text


async def test_an_unknown_entry_slug_is_a_404(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "nothing_here"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 404, response.text


async def test_the_write_is_csrf_protected(
    catalog_routes: CatalogRoutes, session: AsyncSession
) -> None:
    await _publish(session, {"slug": "kestrel", "name": "Kestrel"})
    connection = await _connection(
        session, catalog_routes.workspace_id, mcp_tools=[_tool("read_thing", "read")]
    )

    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(connection.id), "slug": "kestrel"},
    )

    assert response.status_code == 403, response.text


async def test_the_request_body_is_closed(catalog_routes: CatalogRoutes) -> None:
    response = await catalog_routes.client.post(
        _floor_url(catalog_routes.workspace_id),
        json={"connection_id": str(uuid4()), "slug": "kestrel", "floor": "read"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 422, "the floor is derived, never chosen by the caller"
