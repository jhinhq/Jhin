"""Manifest lookups, registry behavior, and the combined tool catalog."""

import pytest

from jhin_connectors import (
    ConnectorError,
    ConnectorRegistry,
    build_default_catalog,
    default_registry,
)
from jhin_connectors.example.connector import ExampleConnector
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.manifest import GITHUB_CAPABILITIES


def test_default_registry_ships_github() -> None:
    registry = default_registry()
    assert "github" in registry.types()
    connector = registry.get("github")
    assert connector is not None
    assert connector.manifest.display_name == "GitHub"
    assert connector.manifest.supports_webhooks


def test_registry_rejects_duplicate_type() -> None:
    registry = ConnectorRegistry()
    registry.register(GitHubConnector())
    with pytest.raises(ConnectorError, match="already registered"):
        registry.register(GitHubConnector())


def test_manifest_auth_scheme_lookup_and_required_fields() -> None:
    manifest = GitHubConnector.manifest
    pat = manifest.auth_scheme("pat")
    assert pat is not None
    assert pat.required_field_names() == ("token",)
    app = manifest.auth_scheme("github_app")
    assert app is not None
    assert set(app.required_field_names()) == {"app_id", "private_key", "installation_id"}
    assert manifest.auth_scheme("oauth") is None


def test_default_catalog_contains_builtins_and_github_tools() -> None:
    catalog = build_default_catalog()
    names = set(catalog.registry.names())
    assert "system.echo" in names  # Phase 4 built-ins survive
    for expected in (
        "github.repository.read",
        "github.branch.list",
        "github.file.read",
        "github.branch.create",
        "github.issue.read",
        "github.issue.comment",
        "github.pull_request.create",
        "github.pull_request.read",
        "github.pull_request.comment",
        "github.pull_request.merge",
        "github.check.read",
        "github.workflow.dispatch",
        "github.workflow_run.read",
    ):
        assert expected in names


def test_github_risk_levels_match_plan() -> None:
    catalog = build_default_catalog()
    expectations = {
        "github.repository.read": ("read", False),
        "github.branch.create": ("write", True),
        "github.pull_request.create": ("write", True),
        "github.pull_request.merge": ("elevated", True),
        "github.workflow.dispatch": ("write", True),
        "github.workflow_run.read": ("read", False),
    }
    for name, (risk, approvable) in expectations.items():
        definition = catalog.registry.get(name)
        assert definition is not None, name
        assert definition.risk.value == risk, name
        assert definition.supports_approval is approvable, name
        # Connection-scoped: every GitHub tool declares scope keys.
        assert "connection_id" in definition.scope_keys
        assert "repository" in definition.scope_keys


def test_manifest_capabilities_cover_all_tools() -> None:
    connector = GitHubConnector()
    tool_capabilities = {definition.required_capability for definition, _ in connector.tools()}
    assert tool_capabilities == set(GITHUB_CAPABILITIES)


def test_example_connector_is_registrable_alongside_github() -> None:
    registry = ConnectorRegistry()
    registry.register(GitHubConnector())
    registry.register(ExampleConnector())
    catalog = build_default_catalog(registry)
    assert catalog.registry.get("example.ping") is not None
    assert catalog.registry.get("github.repository.read") is not None
