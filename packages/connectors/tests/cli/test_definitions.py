"""CLI tool declarations: capabilities, risk levels, scope keys, approval
support, and input-schema strictness (plan 11.6, 48.2)."""

import pytest
from pydantic import ValidationError

from jhin_connectors import build_default_catalog
from jhin_connectors.cli.manifest import CLI_CAPABILITIES, CLI_MANIFEST
from jhin_connectors.cli.schemas import (
    CommandExecuteInput,
    FileReadInput,
    FileWriteInput,
    RepositoryCheckoutInput,
)
from jhin_connectors.cli.tools import CLI_TOOLS
from jhin_policy import RiskLevel


class TestDeclarations:
    def test_five_capabilities_all_registered_in_default_catalog(self) -> None:
        catalog = build_default_catalog()
        names = {definition.name for definition in catalog.definitions()}
        assert set(CLI_CAPABILITIES) <= names
        assert len(CLI_TOOLS) == 5

    def test_risk_levels_and_approval_support(self) -> None:
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        assert by_name["cli.command.execute"].risk is RiskLevel.WRITE
        assert by_name["cli.command.execute"].supports_approval
        assert by_name["cli.repository.checkout"].risk is RiskLevel.WRITE
        assert by_name["cli.repository.checkout"].supports_approval
        assert by_name["cli.file.write"].risk is RiskLevel.WRITE
        assert by_name["cli.file.write"].supports_approval
        assert by_name["cli.test.run"].risk is RiskLevel.READ
        assert by_name["cli.file.read"].risk is RiskLevel.READ

    def test_scope_keys_cover_policy_dimensions(self) -> None:
        """Plan 11.6: policy can constrain image, repository, network,
        command patterns."""
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        assert by_name["cli.command.execute"].scope_keys == (
            "connection_id",
            "command",
            "image",
            "network",
        )
        assert "repository" in by_name["cli.repository.checkout"].scope_keys
        assert "command" in by_name["cli.test.run"].scope_keys
        assert "path" in by_name["cli.file.read"].scope_keys
        assert "path" in by_name["cli.file.write"].scope_keys

    def test_manifest_has_no_secret_fields(self) -> None:
        assert CLI_MANIFEST.auth_schemes[0].type == "none"
        assert CLI_MANIFEST.auth_schemes[0].secret_fields == ()
        assert not CLI_MANIFEST.supports_webhooks


class TestInputValidation:
    def test_absolute_and_traversal_paths_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileReadInput(connection_id="c", path="/etc/passwd")
        with pytest.raises(ValidationError):
            FileReadInput(connection_id="c", path="../outside")
        with pytest.raises(ValidationError):
            FileWriteInput(connection_id="c", path="a/../../b", content="x")
        assert FileReadInput(connection_id="c", path="src/app.py").path == "src/app.py"

    def test_timeout_capped_below_activity_envelope(self) -> None:
        with pytest.raises(ValidationError):
            CommandExecuteInput(connection_id="c", command="sleep 1", timeout_seconds=900)
        ok = CommandExecuteInput(connection_id="c", command="sleep 1", timeout_seconds=480)
        assert ok.timeout_seconds == 480

    def test_repository_shape_enforced(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryCheckoutInput(connection_id="c", repository="not a repo")
        ok = RepositoryCheckoutInput(connection_id="c", repository="octo/alpha")
        assert ok.repository == "octo/alpha"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CommandExecuteInput(connection_id="c", command="ls", host_mount="/")  # type: ignore[call-arg]
