"""Manifest lookups, typed settings, registry behavior, and the tool catalog."""

from typing import Any

import pytest
from pydantic import ValidationError

from jhin_connectors import (
    AuthSchemeSpec,
    ConfigFieldSpec,
    ConnectorError,
    ConnectorManifest,
    ConnectorRegistry,
    SecretFieldSpec,
    build_default_catalog,
    build_default_definition_catalog,
    default_registry,
    normalize_config,
)
from jhin_connectors.cli.connector import CliConnector
from jhin_connectors.example.connector import ExampleConnector
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.manifest import GITHUB_CAPABILITIES
from jhin_connectors.linear.connector import LinearConnector
from jhin_connectors.linear.manifest import LINEAR_MANIFEST
from jhin_connectors.supabase.connector import SupabaseConnector
from jhin_connectors.vercel.connector import VercelConnector
from jhin_policy import ToolDefinition


def _typed_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_type="typed",
        display_name="Typed settings",
        icon="settings",
        auth_schemes=(
            AuthSchemeSpec(
                type="management_token",
                label="Management token",
                secret_fields=(SecretFieldSpec(name="token", label="Token"),),
            ),
            AuthSchemeSpec(
                type="postgres",
                label="PostgreSQL",
                secret_fields=(SecretFieldSpec(name="dsn", label="DSN"),),
            ),
        ),
        config_fields=(
            ConfigFieldSpec(
                name="management_base_url",
                label="Management API origin",
                auth_types=("management_token",),
                default="https://api.example.test",
            ),
            ConfigFieldSpec(
                name="allowed_schemas",
                label="Allowed schemas",
                kind="string_list",
                auth_types=("postgres",),
                default=["public"],
            ),
            ConfigFieldSpec(
                name="statement_timeout_ms",
                label="Statement timeout",
                kind="integer",
                auth_types=("postgres",),
                default=5_000,
                minimum=250,
                maximum=30_000,
            ),
            ConfigFieldSpec(
                name="allow_writes",
                label="Allow writes",
                kind="boolean",
                auth_types=("postgres",),
                default=False,
            ),
        ),
    )


def test_config_fields_filter_by_auth_and_apply_typed_defaults() -> None:
    normalized = normalize_config(
        _typed_manifest(),
        "postgres",
        {"allowed_schemas": ["public"], "statement_timeout_ms": "5000"},
    )

    assert normalized == {
        "allowed_schemas": ["public"],
        "statement_timeout_ms": 5_000,
        "allow_writes": False,
    }
    assert "management_base_url" not in normalized


def test_normalize_config_rejects_fields_for_another_auth_type() -> None:
    with pytest.raises(ValueError, match="management_base_url"):
        normalize_config(
            _typed_manifest(),
            "postgres",
            {"management_base_url": "https://api.example.test"},
        )


@pytest.mark.parametrize(
    ("submitted", "message"),
    [
        ({"statement_timeout_ms": "249"}, "statement_timeout_ms"),
        ({"statement_timeout_ms": True}, "statement_timeout_ms"),
        ({"allow_writes": "sometimes"}, "allow_writes"),
        ({"allowed_schemas": ["public", 7]}, "allowed_schemas"),
        ({"allowed_schemas": ["public", ""]}, "allowed_schemas"),
    ],
)
def test_normalize_config_rejects_invalid_typed_values(
    submitted: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_config(_typed_manifest(), "postgres", submitted)


def test_normalize_config_enforces_required_fields_and_known_auth_types() -> None:
    manifest = _typed_manifest().model_copy(
        update={
            "config_fields": (
                ConfigFieldSpec(name="project_ref", label="Project ref", required=True),
            )
        }
    )

    with pytest.raises(ValueError, match="project_ref"):
        normalize_config(manifest, "postgres", {})
    with pytest.raises(ValueError, match="project_ref"):
        normalize_config(manifest, "postgres", {"project_ref": ""})
    with pytest.raises(ValueError, match="auth type"):
        normalize_config(manifest, "unknown", {"project_ref": "abc"})


def test_boolean_normalization_accepts_only_booleans_and_exact_boolean_strings() -> None:
    manifest = _typed_manifest()

    assert normalize_config(manifest, "postgres", {"allow_writes": True})["allow_writes"] is True
    assert normalize_config(manifest, "postgres", {"allow_writes": "true"})["allow_writes"] is True
    assert (
        normalize_config(manifest, "postgres", {"allow_writes": "false"})["allow_writes"] is False
    )

    with pytest.raises(ValueError, match="allow_writes"):
        normalize_config(manifest, "postgres", {"allow_writes": "TRUE"})


def test_config_fields_cannot_disguise_secret_material() -> None:
    with pytest.raises(ValidationError, match="secret"):
        ConfigFieldSpec(name="token", label="Token", secret=True)  # type: ignore[call-arg]


def test_connector_validate_settings_returns_an_independent_copy() -> None:
    submitted = {"base_url": "https://api.github.com"}

    validated = GitHubConnector().validate_settings("pat", submitted)
    validated["base_url"] = "https://changed.example"

    assert submitted == {"base_url": "https://api.github.com"}


@pytest.mark.parametrize(
    ("connector", "auth_type", "official_origin"),
    [
        (GitHubConnector(), "pat", "https://api.github.com"),
        (LinearConnector(), "api_key", "https://api.linear.app"),
    ],
)
def test_provider_base_url_manifests_have_typed_official_defaults(
    connector: GitHubConnector | LinearConnector,
    auth_type: str,
    official_origin: str,
) -> None:
    field = next(field for field in connector.manifest.config_fields if field.name == "base_url")

    assert field.kind == "text"
    assert field.default == official_origin
    assert normalize_config(connector.manifest, auth_type, {}) == {"base_url": official_origin}


@pytest.mark.parametrize(
    ("connector", "auth_type", "allowed_origin", "submitted"),
    [
        (
            GitHubConnector(),
            "pat",
            "http://fake-github:8080",
            "HTTP://FAKE-GITHUB:8080/",
        ),
        (
            LinearConnector(),
            "api_key",
            "http://fake-linear:8080",
            "HTTP://FAKE-LINEAR:8080/",
        ),
    ],
)
def test_provider_connectors_normalize_approved_base_urls(
    monkeypatch: pytest.MonkeyPatch,
    connector: GitHubConnector | LinearConnector,
    auth_type: str,
    allowed_origin: str,
    submitted: str,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", allowed_origin)

    assert connector.validate_settings(auth_type, {"base_url": submitted}) == {
        "base_url": allowed_origin
    }


@pytest.mark.parametrize(
    ("connector", "auth_type", "submitted", "marker"),
    [
        (
            GitHubConnector(),
            "pat",
            "https://url-userinfo-marker@api.github.com",
            "url-userinfo-marker",
        ),
        (
            GitHubConnector(),
            "github_app",
            "https://api.github.com?token=query-secret-marker",
            "query-secret-marker",
        ),
        (
            GitHubConnector(),
            "pat",
            "http://unapproved-github-marker.invalid",
            "unapproved-github-marker",
        ),
        (
            LinearConnector(),
            "api_key",
            "https://url-userinfo-marker@api.linear.app",
            "url-userinfo-marker",
        ),
        (
            LinearConnector(),
            "oauth",
            "https://api.linear.app?token=query-secret-marker",
            "query-secret-marker",
        ),
        (
            LinearConnector(),
            "api_key",
            "http://unapproved-linear-marker.invalid",
            "unapproved-linear-marker",
        ),
    ],
)
def test_provider_connectors_reject_unsafe_base_urls_without_echoing_values(
    connector: GitHubConnector | LinearConnector,
    auth_type: str,
    submitted: str,
    marker: str,
) -> None:
    config = {"base_url": submitted}

    with pytest.raises(ValueError) as exc_info:
        connector.validate_settings(auth_type, config)

    assert marker not in str(exc_info.value)
    assert config == {"base_url": submitted}


def test_webhook_manifest_mode_is_authoritative_and_legacy_boolean_is_derived() -> None:
    manifest = ConnectorManifest(
        connector_type="provider-webhook",
        display_name="Provider webhook",
        icon="webhook",
        webhook_events=("deployment.ready",),
        webhook_secret_mode="provider_supplied",
        webhook_signature_algorithm="hmac-sha1",
        webhook_setup_help="Copy the signing secret from the provider.",
    )

    assert manifest.supports_webhooks is True
    assert manifest.webhook_secret_mode == "provider_supplied"

    with pytest.raises(ValidationError, match="supports_webhooks"):
        ConnectorManifest(
            connector_type="invalid",
            display_name="Invalid",
            icon="invalid",
            webhook_events=("ping",),
            webhook_secret_mode="generated",
            supports_webhooks=False,
        )


def test_manifest_rejects_events_without_a_webhook_secret_mode() -> None:
    with pytest.raises(ValidationError, match="webhook_secret_mode"):
        ConnectorManifest(
            connector_type="invalid",
            display_name="Invalid",
            icon="invalid",
            webhook_events=("ping",),
            webhook_secret_mode="none",
        )


def test_github_and_linear_use_generated_hmac_sha256_webhook_secrets() -> None:
    github = GitHubConnector.manifest

    assert github.webhook_secret_mode == "generated"
    assert github.webhook_signature_algorithm == "hmac-sha256"
    assert LINEAR_MANIFEST.webhook_secret_mode == "generated"
    assert LINEAR_MANIFEST.webhook_signature_algorithm == "hmac-sha256"


def test_default_registry_ships_github() -> None:
    registry = default_registry()
    assert "github" in registry.types()
    connector = registry.get("github")
    assert connector is not None
    assert connector.manifest.display_name == "GitHub"
    assert connector.manifest.supports_webhooks


def test_default_registry_ships_vercel_with_webhook_ingress() -> None:
    registry = default_registry()
    connector = registry.get("vercel")

    assert isinstance(connector, VercelConnector)
    assert connector.manifest.supports_webhooks is True
    assert connector.manifest.webhook_secret_mode == "provider_supplied"
    assert len(connector.manifest.webhook_events) == 6
    assert len(connector.manifest.canonical_events) == 5


def test_default_registry_ships_supabase_exactly_once() -> None:
    registry = default_registry()
    connector = registry.get("supabase")

    assert isinstance(connector, SupabaseConnector)
    assert registry.types().count("supabase") == 1
    assert connector.manifest.webhook_secret_mode == "none"
    assert connector.manifest.supports_webhooks is False
    assert {definition.name for definition, _ in connector.tools()} == {
        "supabase.project.read",
        "supabase.logs.read",
        "supabase.function.list",
        "supabase.function.deploy",
        "supabase.function.delete",
        "supabase.database.read",
        "supabase.database.write",
        "supabase.database.destructive",
    }


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
    # Signing in with GitHub asks for nothing: the tokens arrive from the
    # callback, so the scheme declares no field for anyone to fill in.
    signed_in = manifest.auth_scheme("oauth")
    assert signed_in is not None
    assert signed_in.required_field_names() == ()
    assert manifest.auth_scheme("carrier_pigeon") is None


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


def test_default_definition_catalog_matches_executable_definitions() -> None:
    executable_names = tuple(
        definition.name for definition in build_default_catalog().definitions()
    )

    definition_names = tuple(
        definition.name for definition in build_default_definition_catalog().definitions()
    )

    assert definition_names == executable_names


@pytest.mark.parametrize(
    "connector",
    [
        GitHubConnector(),
        CliConnector(),
        LinearConnector(),
        VercelConnector(),
        SupabaseConnector(),
        ExampleConnector(),
    ],
)
def test_connectors_expose_definitions_without_executor_callables(connector: Any) -> None:
    definitions = connector.tool_definitions()

    assert definitions
    assert all(isinstance(definition, ToolDefinition) for definition in definitions)
    assert tuple(definition.name for definition in definitions) == tuple(
        definition.name for definition, _executor in connector.tools()
    )


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
    assert ExampleConnector.manifest.webhook_secret_mode == "generated"
