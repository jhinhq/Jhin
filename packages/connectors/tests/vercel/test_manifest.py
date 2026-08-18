"""Static Vercel connector contracts and policy behavior."""

import pytest
from pydantic import ValidationError

from jhin_connectors.registry import build_default_catalog, default_registry
from jhin_connectors.vercel.connector import VercelConnector
from jhin_connectors.vercel.schemas import PreviewCreateInput
from jhin_policy import (
    ApprovalPreset,
    DecisionType,
    Grant,
    PolicyRule,
    RiskLevel,
    RuleAction,
    evaluate,
    rules_for_preset,
)

EXPECTED_TOOLS = {
    "vercel.project.list",
    "vercel.project.read",
    "vercel.deployment.list",
    "vercel.deployment.read",
    "vercel.deployment.logs.read",
    "vercel.environment_metadata.read",
    "vercel.deployment.preview.create",
    "vercel.deployment.redeploy",
    "vercel.deployment.promote",
    "vercel.deployment.alias.assign",
}


def test_manifest_declares_access_token_config_and_no_webhook_yet() -> None:
    manifest = VercelConnector.manifest

    assert manifest.connector_type == "vercel"
    assert manifest.display_name == "Vercel"
    assert len(manifest.auth_schemes) == 1
    scheme = manifest.auth_schemes[0]
    assert scheme.type == "access_token"
    assert scheme.required_field_names() == ("token",)
    fields = {field.name: field for field in manifest.config_fields}
    assert fields["team_id"].required is False
    assert fields["base_url"].default == "https://api.vercel.com"
    assert manifest.webhook_secret_mode == "none"
    assert manifest.webhook_events == ()
    assert manifest.supports_webhooks is False


def test_manifest_and_connector_tools_match_exactly() -> None:
    connector = VercelConnector()
    definitions = {definition.name: definition for definition, _ in connector.tools()}

    assert set(definitions) == EXPECTED_TOOLS
    assert set(connector.manifest.capabilities) == EXPECTED_TOOLS


def test_default_registry_and_catalog_ship_vercel() -> None:
    registry = default_registry()
    connector = registry.get("vercel")

    assert connector is not None
    assert set(EXPECTED_TOOLS).issubset(build_default_catalog().registry.names())


def test_tool_scope_contracts_are_fixed() -> None:
    definitions = {definition.name: definition for definition, _ in VercelConnector().tools()}
    expected = {
        "vercel.project.list": ({"connection_id"}, {"connection_id"}),
        "vercel.project.read": (
            {"connection_id", "project_id"},
            {"connection_id", "project_id"},
        ),
        "vercel.deployment.list": (
            {"connection_id", "project_id"},
            {"connection_id", "project_id"},
        ),
        "vercel.deployment.read": (
            {"connection_id", "project_id", "deployment_id"},
            {"connection_id", "project_id", "deployment_id"},
        ),
        "vercel.deployment.logs.read": (
            {"connection_id", "project_id", "deployment_id"},
            {"connection_id", "project_id", "deployment_id"},
        ),
        "vercel.environment_metadata.read": (
            {"connection_id", "project_id"},
            {"connection_id", "project_id"},
        ),
        "vercel.deployment.preview.create": (
            {"connection_id", "project_id", "environment", "repository_id", "ref"},
            {"connection_id", "project_id", "environment", "repository_id"},
        ),
        "vercel.deployment.redeploy": (
            {"connection_id", "project_id", "deployment_id", "environment"},
            {"connection_id", "project_id", "deployment_id", "environment"},
        ),
        "vercel.deployment.promote": (
            {"connection_id", "project_id", "deployment_id", "environment"},
            {"connection_id", "project_id", "deployment_id", "environment"},
        ),
        "vercel.deployment.alias.assign": (
            {"connection_id", "project_id", "deployment_id", "environment", "alias"},
            {"connection_id", "project_id", "deployment_id", "environment", "alias"},
        ),
    }

    for name, (scope_keys, required_keys) in expected.items():
        assert set(definitions[name].scope_keys) == scope_keys, name
        assert set(definitions[name].required_grant_scope_keys) == required_keys, name


def test_preview_is_elevated_and_other_mutations_are_destructive() -> None:
    definitions = {definition.name: definition for definition, _ in VercelConnector().tools()}

    assert definitions["vercel.deployment.preview.create"].risk is RiskLevel.ELEVATED
    for name in (
        "vercel.deployment.redeploy",
        "vercel.deployment.promote",
        "vercel.deployment.alias.assign",
    ):
        assert definitions[name].risk is RiskLevel.DESTRUCTIVE
    for name in (
        "vercel.deployment.preview.create",
        "vercel.deployment.redeploy",
        "vercel.deployment.promote",
        "vercel.deployment.alias.assign",
    ):
        assert definitions[name].supports_approval is True


def test_preview_input_cannot_target_production() -> None:
    valid = {
        "connection_id": "connection",
        "project_id": "project",
        "git_provider": "github",
        "repository_id": "octo/widgets",
        "ref": "feature/safe",
    }
    assert PreviewCreateInput(**valid).environment == "preview"

    with pytest.raises(ValidationError):
        PreviewCreateInput(**valid, environment="production")


@pytest.mark.parametrize(
    ("tool_name", "autonomous_result"),
    [
        ("vercel.deployment.preview.create", DecisionType.ALLOW),
        ("vercel.deployment.redeploy", DecisionType.REQUIRE_APPROVAL),
        ("vercel.deployment.promote", DecisionType.REQUIRE_APPROVAL),
        ("vercel.deployment.alias.assign", DecisionType.REQUIRE_APPROVAL),
    ],
)
def test_mutation_policy_defaults_and_explicit_auto(
    tool_name: str, autonomous_result: DecisionType
) -> None:
    definition = next(
        definition for definition, _ in VercelConnector().tools() if definition.name == tool_name
    )
    requested = {key: f"scope-{key}" for key in definition.scope_keys}
    if tool_name == "vercel.deployment.preview.create":
        requested["environment"] = "preview"
    elif tool_name in {
        "vercel.deployment.promote",
        "vercel.deployment.alias.assign",
    }:
        requested["environment"] = "production"
    grant_scope = {key: requested[key] for key in definition.required_grant_scope_keys}
    grant = Grant(capability=definition.required_capability, scope=grant_scope)

    balanced = evaluate(
        definition,
        grants=[grant],
        rules=rules_for_preset(ApprovalPreset.BALANCED),
        requested_scope=requested,
    )
    autonomous = evaluate(
        definition,
        grants=[grant],
        rules=rules_for_preset(ApprovalPreset.AUTONOMOUS),
        requested_scope=requested,
    )
    explicit_auto = evaluate(
        definition,
        grants=[grant],
        rules=[PolicyRule(capability=tool_name, action=RuleAction.AUTO)],
        requested_scope=requested,
    )

    assert balanced.decision is DecisionType.REQUIRE_APPROVAL
    assert autonomous.decision is autonomous_result
    assert explicit_auto.decision is DecisionType.ALLOW
