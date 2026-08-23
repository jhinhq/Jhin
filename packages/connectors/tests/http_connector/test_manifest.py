"""Manifest, registration, and risk mapping for the generic HTTP connector."""

from __future__ import annotations

import pytest

from jhin_connectors import build_default_catalog, default_registry, normalize_config
from jhin_connectors.http.connector import HttpConnector
from jhin_connectors.http.manifest import HTTP_CAPABILITIES, HTTP_MANIFEST


def test_default_registry_ships_http() -> None:
    registry = default_registry()
    connector = registry.get("http")
    assert isinstance(connector, HttpConnector)
    assert connector.manifest.display_name == "Any HTTP API"
    assert connector.manifest.supports_webhooks is False
    assert connector.manifest.webhook_secret_mode == "none"


def test_auth_schemes_cover_none_bearer_header_and_basic() -> None:
    schemes = {scheme.type for scheme in HTTP_MANIFEST.auth_schemes}
    assert schemes == {"none", "bearer", "header", "basic"}
    basic = HTTP_MANIFEST.auth_scheme("basic")
    assert basic is not None
    assert set(basic.required_field_names()) == {"username", "password"}
    header = HTTP_MANIFEST.auth_scheme("header")
    assert header is not None
    assert header.required_field_names() == ("token",)


def test_base_url_is_required_and_header_name_scoped_to_header_auth() -> None:
    with pytest.raises(ValueError, match="base_url"):
        normalize_config(HTTP_MANIFEST, "none", {})
    normalized = normalize_config(HTTP_MANIFEST, "none", {"base_url": "https://api.example.com"})
    assert normalized == {"base_url": "https://api.example.com"}
    with pytest.raises(ValueError, match="header_name"):
        normalize_config(
            HTTP_MANIFEST, "bearer", {"base_url": "https://x.example", "header_name": "X-K"}
        )
    with pytest.raises(ValueError, match="header_name"):
        normalize_config(HTTP_MANIFEST, "header", {"base_url": "https://x.example"})


def test_risk_split_read_get_write_request() -> None:
    catalog = build_default_catalog()
    get_definition = catalog.registry.get("http.get")
    request_definition = catalog.registry.get("http.request")
    assert get_definition is not None and request_definition is not None
    assert get_definition.risk.value == "read"
    assert get_definition.supports_approval is False
    assert request_definition.risk.value == "write"
    assert request_definition.supports_approval is True
    for definition in (get_definition, request_definition):
        assert definition.scope_keys == ("connection_id", "method", "path")


def test_manifest_capabilities_cover_all_tools() -> None:
    connector = HttpConnector()
    tool_capabilities = {definition.required_capability for definition, _ in connector.tools()}
    assert tool_capabilities == set(HTTP_CAPABILITIES)
    assert tuple(definition.name for definition in connector.tool_definitions()) == (
        "http.get",
        "http.request",
    )
