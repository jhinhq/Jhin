"""``sign_in``: the curated answer to how a person connects each app.

The field decides routing in the browser, so the tests that matter are the
ones about honesty — that an entry claiming "sign in with your account" has
somewhere to send you, and that the apps whose remote servers really do sign
people in are the ones marked for it.
"""

import pytest

from jhin_connectors.catalog import CatalogApp, load_catalog


def catalog_by_slug(slug: str) -> CatalogApp | None:
    """The one entry with this slug, or None.

    Local to the tests on purpose: these assertions are about the shipped
    data, so they read it the same way production does — through
    ``load_catalog`` — rather than through a lookup helper whose own
    correctness would then be part of what is under test.
    """
    return next((entry for entry in load_catalog() if entry.slug == slug), None)


# The curated apps whose own remote MCP server signs people in. Each is a
# provider Jhin can complete OAuth with today; none of them should ever be
# offering a token box.
REMOTE_MCP_SLUGS = frozenset(
    {
        "asana",
        "atlassian",
        "canva",
        "figma",
        "linear",
        "neon",
        "notion",
        "sentry",
        "supabase",
        "vercel",
    }
)

_BASE = {
    "slug": "acme",
    "name": "Acme",
    "category": "Developer tools",
    "icon": "mcp",
    "description": "Acme tools.",
}


def test_the_catalog_still_loads_and_leaves_the_rest_on_auto() -> None:
    entries = load_catalog()
    assert len(entries) >= 40
    assert {entry.slug for entry in entries if entry.sign_in == "remote_mcp"} == REMOTE_MCP_SLUGS
    # GitHub has both a native connector and a known remote endpoint, and its
    # remote server still wants a personal access token: exactly the case the
    # default heuristic already gets right.
    untouched = {entry.slug for entry in entries if entry.sign_in == "auto"}
    assert {"github", "slack", "stripe"} <= untouched
    assert not untouched & REMOTE_MCP_SLUGS


def test_the_remote_mcp_apps_are_marked_and_can_be_signed_in_to() -> None:
    for slug in REMOTE_MCP_SLUGS:
        entry = catalog_by_slug(slug)
        assert entry is not None, slug
        assert entry.sign_in == "remote_mcp", slug
        # What the mark promises: a confirmed endpoint whose discovery Jhin
        # can run, and an OAuth scheme to run against it.
        assert entry.mcp_url is not None and entry.mcp_url.startswith("https://"), slug
        assert not entry.url_unverified and not entry.stdio_only, slug
        assert entry.auth_hint == "oauth", slug


def test_a_pasted_key_is_stated_as_a_fact_about_the_provider() -> None:
    """The ``key`` entries are the ones with no sign-in to offer, so the card
    should say so rather than implying a redirect that does not exist."""
    for slug in ("brave_search", "exa", "firecrawl", "huggingface", "resend", "tavily", "twilio"):
        entry = catalog_by_slug(slug)
        assert entry is not None and entry.sign_in == "key", slug
        assert entry.auth_hint != "oauth", slug
    for slug in ("web_search_brave", "web_search_exa", "web_search_tavily"):
        entry = catalog_by_slug(slug)
        assert entry is not None and entry.sign_in == "key" and entry.connector_type == "web", slug


def test_the_public_docs_servers_need_no_account() -> None:
    for slug in ("cloudflare", "context7", "deepwiki", "microsoft_learn"):
        entry = catalog_by_slug(slug)
        assert entry is not None and entry.sign_in == "none", slug
        assert entry.auth_hint == "none", slug


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"mcp_url": "https://mcp.acme.example/mcp", "url_unverified": True},
        {"mcp_url": "https://mcp.acme.example/mcp", "stdio_only": True},
    ],
    ids=["no endpoint", "unverified endpoint", "stdio only"],
)
def test_remote_mcp_without_a_verified_endpoint_is_refused(broken: dict[str, object]) -> None:
    """Marking an entry ``remote_mcp`` sends people to the provider to sign in.
    An entry with nowhere to send them would promise a redirect and then show
    a blank URL form, so the catalog refuses to load rather than ship it."""
    with pytest.raises(ValueError):
        CatalogApp.model_validate({**_BASE, "sign_in": "remote_mcp", **broken})

    entry = CatalogApp.model_validate(
        {**_BASE, "sign_in": "remote_mcp", "mcp_url": "https://mcp.acme.example/mcp"}
    )
    assert entry.sign_in == "remote_mcp"


def test_no_sign_in_needed_must_agree_with_the_auth_hint() -> None:
    with pytest.raises(ValueError):
        CatalogApp.model_validate({**_BASE, "sign_in": "none", "auth_hint": "bearer"})
    assert CatalogApp.model_validate({**_BASE, "sign_in": "none", "auth_hint": "none"})


def test_unknown_sign_in_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        CatalogApp.model_validate({**_BASE, "sign_in": "sso"})


def test_connectable_is_unchanged_by_the_new_field() -> None:
    """Readers still ask ``connectable`` whether the Connect button does
    anything; ``sign_in`` only changes where it goes."""
    stdio = CatalogApp.model_validate({**_BASE, "stdio_only": True, "url_unverified": True})
    assert not stdio.connectable
    resend = catalog_by_slug("resend")
    assert resend is not None and resend.sign_in == "key" and not resend.connectable
    assert all(entry.connectable for entry in load_catalog() if entry.sign_in == "remote_mcp")
