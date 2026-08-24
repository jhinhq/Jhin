"""Manifest, registry, and Apps-library catalog entries for the web connector."""

from __future__ import annotations

from jhin_connectors.catalog import load_catalog
from jhin_connectors.manifest import normalize_config
from jhin_connectors.registry import build_default_catalog, default_registry
from jhin_connectors.web.manifest import WEB_MANIFEST
from jhin_policy import RiskLevel


def test_registry_includes_the_web_connector() -> None:
    registry = default_registry()
    connector = registry.get("web")
    assert connector is not None
    assert connector.manifest.capabilities == ("web.search", "web.fetch")
    assert connector.manifest.supports_webhooks is False


def test_catalog_registers_both_tools_with_read_risk_and_scopes() -> None:
    catalog = build_default_catalog()
    search = catalog.registry.get("web.search")
    fetch = catalog.registry.get("web.fetch")
    assert search is not None and fetch is not None
    assert search.risk is RiskLevel.READ
    assert fetch.risk is RiskLevel.READ
    assert search.scope_keys == ("connection_id",)
    assert fetch.scope_keys == ("connection_id", "domain")
    assert search.supports_approval is False


def test_auth_schemes_cover_fetch_only_and_bearer() -> None:
    types = [scheme.type for scheme in WEB_MANIFEST.auth_schemes]
    assert types == ["none", "bearer"]
    bearer = WEB_MANIFEST.auth_scheme("bearer")
    assert bearer is not None
    assert bearer.required_field_names() == ("token",)


def test_normalize_config_defaults_the_backend_for_bearer() -> None:
    normalized = normalize_config(WEB_MANIFEST, "bearer", {})
    assert normalized["search_backend"] == "tavily"
    # Fetch-only connections have no backend field at all.
    assert "search_backend" not in normalize_config(WEB_MANIFEST, "none", {})


def test_apps_library_lists_all_three_backends() -> None:
    by_slug = {entry.slug: entry for entry in load_catalog()}
    for slug, backend in (
        ("web_search_tavily", "tavily"),
        ("web_search_brave", "brave"),
        ("web_search_exa", "exa"),
    ):
        entry = by_slug[slug]
        assert entry.connector_type == "web"
        assert entry.category == "Search & web"
        assert entry.connector_config == {"search_backend": backend}
    dev = by_slug["fake_websearch"]
    assert dev.connector_config["base_url"] == "http://fake-websearch:8080"
