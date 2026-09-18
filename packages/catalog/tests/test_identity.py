"""Browse dedup must not turn nearby app names into an identity match."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from jhin_catalog_sync.identity import AppIdentity, duplicate_app_keys


def app(
    key: str,
    *,
    name: str | None = None,
    endpoint: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> AppIdentity:
    return AppIdentity(
        key=key, slug=key, name=name or key, endpoint=endpoint, metadata=metadata or {}
    )


@pytest.mark.parametrize("name", ["GitHub", " github ", "GIT-HUB", "Official GitHub MCP Server"])
def test_builtin_names_resolve_across_display_spelling(name: str) -> None:
    assert duplicate_app_keys([app("github", name="GitHub")], [app("imported", name=name)]) == (
        "imported",
    )


@pytest.mark.parametrize("name", ["GitHub Analytics", "GitHub Actions", "Not GitHub", "Hub"])
def test_distinct_extensions_do_not_match_a_builtin_name(name: str) -> None:
    assert duplicate_app_keys([app("github", name="GitHub")], [app("imported", name=name)]) == ()


def test_registry_package_identity_links_renamed_community_entries() -> None:
    first = app("first", metadata={"npm_package": "@acme/notes"})
    second = app(
        "second", metadata={"packages": [{"registry_type": "npm", "identifier": "@acme/notes"}]}
    )
    third = app("third", metadata={"npm_package": "@other/notes"})
    assert duplicate_app_keys([], [first, second, third]) == ("second",)


def test_transitive_endpoint_and_repository_evidence_chooses_first_entry() -> None:
    repo = {"host": "github.com", "owner": "acme", "repo": "notes"}
    first = app("first", endpoint="https://notes.example/mcp")
    second = app("second", metadata={"repo": repo})
    bridge = app("bridge", endpoint="https://notes.example/mcp", metadata={"repo": repo})
    assert duplicate_app_keys([], [first, second, bridge]) == ("second", "bridge")


def test_monorepo_subpaths_stay_distinct_even_when_their_display_names_match() -> None:
    repo = {"host": "github.com", "owner": "acme", "repo": "suite"}
    first = app("first", name="Agent MCP", metadata={"repo": {**repo, "subpath": "mail"}})
    second = app("second", name="Agent MCP", metadata={"repo": {**repo, "subpath": "tasks"}})
    assert duplicate_app_keys([], [first, second]) == ()


def test_remote_endpoint_metadata_matches_a_builtin_without_a_projected_url() -> None:
    builtin = app("notion", endpoint="https://mcp.notion.com/mcp")
    imported = app("remote", metadata={"remotes": [{"url": "https://mcp.notion.com/mcp/"}]})
    assert duplicate_app_keys([builtin], [imported]) == ("remote",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://notes.example/other",
        "https://notes.example/MCP",
        "http://notes.example/mcp",
        "https://notes.example/mcp?tenant=other",
        "https://notes.example.evil/mcp",
        "https://user@notes.example/mcp",
        "https://notes.example:444/mcp",
        "not a URL",
    ],
)
def test_endpoint_matching_retains_service_and_tenant_boundaries(endpoint: str) -> None:
    assert (
        duplicate_app_keys(
            [],
            [app("first", endpoint="https://notes.example/mcp"), app("second", endpoint=endpoint)],
        )
        == ()
    )


def test_same_names_on_unrelated_providers_are_not_community_duplicates() -> None:
    assert duplicate_app_keys([], [app("first", name="Notes"), app("second", name="Notes")]) == ()


@pytest.mark.parametrize(
    "name",
    ["GitHub API MCP Server", "@0xshariq/github-mcp-server", "@acme/mcp-server-github"],
)
def test_published_catalog_package_and_api_wrappers_match_builtin(name: str) -> None:
    assert duplicate_app_keys([app("github", name="GitHub")], [app("import", name=name)]) == (
        "import",
    )


@pytest.mark.parametrize(
    "name",
    ["@acme/github-actions-mcp", "@acme/github-security-mcp-server", "GitHub Search API Server"],
)
def test_package_scopes_do_not_hide_distinct_builtin_extensions(name: str) -> None:
    assert duplicate_app_keys([app("github", name="GitHub")], [app("import", name=name)]) == ()


def test_published_smithery_remote_and_directory_record_share_qualified_identity() -> None:
    registry = app(
        "registry",
        name="pinion05-supabase-mcp-lite",
        metadata={
            "remotes": [{"url": "https://server.smithery.ai/@pinion05/supabase-mcp-lite/mcp"}]
        },
    )
    smithery = app(
        "smithery",
        name="supabase-mcp-lite",
        metadata={"smithery_qualified_name": "pinion05/supabase-mcp-lite"},
    )
    assert duplicate_app_keys([], [registry, smithery]) == ("smithery",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://server.smithery.ai/@other/supabase-mcp-lite/mcp",
        "https://server.smithery.ai/@pinion05/another/mcp",
        "https://server.smithery.ai.evil/@pinion05/supabase-mcp-lite/mcp",
        "https://server.smithery.ai/@pinion05/supabase-mcp-lite/mcp?tenant=other",
    ],
)
def test_smithery_qualified_identity_keeps_publisher_and_service_boundaries(endpoint: str) -> None:
    first = app("first", endpoint=endpoint)
    second = app("second", metadata={"smithery_qualified_name": "pinion05/supabase-mcp-lite"})
    assert duplicate_app_keys([], [first, second]) == ()


@pytest.mark.parametrize(
    "name",
    [
        "Supabase Admin",
        "Supabase Admin Self-Hosted",
        "supabase-mcp-cloud-and-selfhosted",
        "selfhosted-supabase-mcp",
        "supabase-mcp-lite",
        "supabase-godmode",
        "@mseep/supabase-godmode",
        "supabase-godmode-v2",
    ],
)
def test_reviewed_supabase_connection_variants_use_the_builtin_card(name: str) -> None:
    assert duplicate_app_keys([app("supabase")], [app("wrapper", name=name)]) == ("wrapper",)


@pytest.mark.parametrize(
    "name",
    [
        "Health Data (your own Supabase)",
        "supabase-ticketing-system",
        "supabase-db-supervisor",
        "shared-supabase-mcp-minimal",
        "@modelfetch/supabase",
    ],
)
def test_supabase_backed_products_and_runtime_adapters_remain_distinct(name: str) -> None:
    assert duplicate_app_keys([app("supabase")], [app("extension", name=name)]) == ()


def test_publisher_prefixed_display_name_uses_exact_repository_evidence() -> None:
    wrapper = app(
        "wrapper",
        name="MisterSandFR-supabase-mcp-selfhosted",
        metadata={
            "repo": {
                "host": "github.com",
                "owner": "mistersandfr",
                "repo": "supabase-mcp-selfhosted",
            }
        },
    )
    assert duplicate_app_keys([app("supabase")], [wrapper]) == ("wrapper",)


def test_scoped_mcp_package_keeps_the_provider_name_when_package_is_only_a_wrapper() -> None:
    assert duplicate_app_keys([app("stripe")], [app("wrapper", name="@stripe/mcp")]) == ("wrapper",)


@pytest.mark.parametrize(
    "builtin, owner, repo, subpath, name",
    [
        ("notion", "smithery-ai", "mcp-servers", "notion", "smithery-notion"),
        ("github", "smithery-ai", "mcp-servers", "github", "smithery-ai-github"),
        ("notion", "pipeworx-io", "mcp-notion_connect", "", "Notion_connect"),
        ("github", "pipeworx-io", "mcp-github_private", "", "Github_private"),
        ("notion", "n24q02m", "better-notion-mcp", "", "better-notion-mcp"),
    ],
)
def test_reviewed_wrappers_resolve_to_builtin_by_exact_repository(
    builtin: str,
    owner: str,
    repo: str,
    subpath: str,
    name: str,
) -> None:
    wrapper = app(
        "wrapper",
        name=name,
        metadata={
            "repo": {
                "host": "github.com",
                "owner": owner,
                "repo": repo,
                "subpath": subpath,
            }
        },
    )
    assert duplicate_app_keys([app(builtin)], [wrapper]) == ("wrapper",)


@pytest.mark.parametrize(
    "owner, repo, subpath, name",
    [
        ("other", "mcp-notion_connect", "", "Notion_connect"),
        ("smithery-ai", "mcp-servers", "notion-analysis", "Notion Analysis"),
        ("smithery-ai", "mcp-servers", "", "Smithery MCP Servers"),
        ("pipeworx-io", "mcp-notion_connect", "analytics", "Notion Analytics"),
        ("other", "better-notion-mcp", "", "better-notion-mcp"),
    ],
)
def test_reviewed_repository_matches_do_not_expand_to_other_products(
    owner: str,
    repo: str,
    subpath: str,
    name: str,
) -> None:
    item = app(
        "extension",
        name=name,
        metadata={
            "repo": {
                "host": "github.com",
                "owner": owner,
                "repo": repo,
                "subpath": subpath,
            }
        },
    )
    assert duplicate_app_keys([app("notion"), app("github")], [item]) == ()


def test_verified_tenpace_repository_transfer_prefers_current_endpoint():
    old = app(
        "old",
        name="Tenpace Review Router",
        endpoint="https://api.tenpace.com/mcp",
        metadata={"repo": {"host": "github.com", "owner": "Patrax", "repo": "review-router-setup"}},
    )
    new = app(
        "new",
        name="Tenpace Review Router",
        endpoint="https://api.tenpace.com/v1/review-router/mcp",
        metadata={
            "repo": {"host": "github.com", "owner": "tenpace-app", "repo": "review-router-setup"}
        },
    )
    assert duplicate_app_keys([], [old, new]) == ("old",)
    assert duplicate_app_keys([], [new, old]) == ("old",)
    # A canonical transfer must never elevate an indexed record over a
    # verified source or take ownership from a reviewed built-in.
    assert duplicate_app_keys([], [replace(old, trust_rank=1), replace(new, trust_rank=4)]) == (
        "new",
    )
    assert duplicate_app_keys([old], [new]) == ("new",)


def test_tenpace_alias_does_not_merge_unrelated_endpoints_or_repository_subpaths():
    first = app(
        "first",
        name="Tenpace Review Router",
        endpoint="https://api.tenpace.com/mcp",
        metadata={
            "repo": {
                "host": "github.com",
                "owner": "patrax",
                "repo": "review-router-setup",
                "subpath": "other",
            }
        },
    )
    second = app(
        "second",
        name="Tenpace Review Router",
        endpoint="https://api.tenpace.com/v1/another/mcp",
        metadata={
            "repo": {"host": "github.com", "owner": "tenpace-app", "repo": "review-router-setup"}
        },
    )
    assert duplicate_app_keys([], [first, second]) == ()


def test_identical_yultrace_names_on_ephemeral_hosts_are_not_identity_evidence():
    first = app("one", name="Yultrace", endpoint="https://one.trycloudflare.com/mcp")
    second = app("two", name="Yultrace", endpoint="https://two.trycloudflare.com/mcp")
    assert duplicate_app_keys([], [first, second]) == ()
