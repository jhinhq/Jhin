"""CLI tool declarations: capabilities, risk levels, scope keys, approval
support, and input-schema strictness (plan 11.6, 48.2)."""

import pytest
from pydantic import ValidationError

from jhin_connectors import build_default_catalog
from jhin_connectors.cli.manifest import CLI_CAPABILITIES, CLI_MANIFEST
from jhin_connectors.cli.schemas import (
    CommandExecuteInput,
    FileEditInput,
    FileListInput,
    FileReadInput,
    FileSearchInput,
    FileWriteInput,
    RepositoryCheckoutInput,
    RepositoryPushInput,
)
from jhin_connectors.cli.tools import CLI_TOOLS
from jhin_policy import RiskLevel

# Every path shape that must never reach git's own state, and the reason each
# one is a distinct case rather than a variation of the last.
GIT_INTERNAL_PATHS = [
    ".git/config",  # the plain form
    ".git",  # the directory itself
    ".GIT/config",  # case-insensitively, for a case-folding filesystem
    "src/.git/config",  # nested, e.g. a submodule's
    "a/b/.git",  # nested, as the leaf
    "./.git/config",  # dressed up with a '.' segment
    ".gitconfig",  # git's per-user config, when no checkout has run yet
    ".gitmodules",  # names where a nested .git may be created
    ".jhin-askpass",  # Jhin's own workspace files
    ".jhinrc",
    ".git\\config",  # a separator the sandbox does not use
]

# Ordinary repository content that happens to start with ".git" and must stay
# readable and writable, or an agent cannot fix a CI workflow.
GIT_ADJACENT_PATHS = [
    ".github/workflows/ci.yml",
    ".gitignore",
    ".gitattributes",
    "src/.gitignore",
]


class TestDeclarations:
    def test_every_capability_is_registered_in_the_default_catalog(self) -> None:
        catalog = build_default_catalog()
        names = {definition.name for definition in catalog.definitions()}
        assert set(CLI_CAPABILITIES) <= names
        assert len(CLI_TOOLS) == len(CLI_CAPABILITIES)

    def test_new_cli_capabilities_are_declared_in_the_manifest(self) -> None:
        declared = set(CLI_CAPABILITIES)
        assert {
            "cli.file.list",
            "cli.file.search",
            "cli.file.edit",
            "cli.repository.push",
        } <= declared
        assert declared == {definition.name for definition, _ in CLI_TOOLS}

    def test_risk_levels_and_approval_support(self) -> None:
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        assert by_name["cli.command.execute"].risk is RiskLevel.WRITE
        assert by_name["cli.command.execute"].supports_approval
        assert by_name["cli.repository.checkout"].risk is RiskLevel.WRITE
        assert by_name["cli.repository.checkout"].supports_approval
        assert by_name["cli.file.write"].risk is RiskLevel.WRITE
        assert by_name["cli.file.write"].supports_approval
        assert by_name["cli.file.edit"].risk is RiskLevel.WRITE
        assert by_name["cli.file.edit"].supports_approval
        assert by_name["cli.file.read"].risk is RiskLevel.READ
        assert by_name["cli.file.list"].risk is RiskLevel.READ
        assert by_name["cli.file.search"].risk is RiskLevel.READ

    def test_test_run_is_a_write_because_it_is_an_arbitrary_shell(self) -> None:
        """It is named after tests, but its input is a shell string and its
        working directory is the checkout, so it can change any file there.
        READ would auto-run it under *every* preset including Restricted, and
        a grant scope is one fnmatch over that string:
        ``fnmatchcase("pytest -x; <anything>", "pytest*")`` is True. Whatever
        it does to the checkout, cli.repository.push trusts none of it — but
        the declared risk has to be honest on its own."""
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        run = by_name["cli.test.run"]
        assert run.risk is RiskLevel.WRITE
        # WRITE under Restricted is APPROVAL, and an approval a tool cannot
        # support is a denial, so the two must move together.
        assert run.supports_approval

    def test_push_is_elevated_and_supports_approval(self) -> None:
        """The one call that leaves the sandbox is the one a human sees.
        ELEVATED is APPROVAL under the balanced preset (the wizard default)
        and under the risk defaults; the Code-editing preset also ships an
        explicit capability rule so Autonomous does not skip it."""
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        push = by_name["cli.repository.push"]
        assert push.risk is RiskLevel.ELEVATED
        assert push.supports_approval

    def test_checkout_and_push_require_repository_scope(self) -> None:
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        checkout = by_name["cli.repository.checkout"]
        assert checkout.required_grant_scope_keys == ("connection_id", "repository")

    def test_push_requires_the_branch_scope_not_merely_offers_it(self) -> None:
        """``scope_matches`` only checks the keys a *grant* constrains, so a
        grant of {connection_id, repository} matched a push of any branch —
        the in-sandbox refusals (main/master/HEAD and the recorded base) were
        the entire limit. Which branches an agent may land on is a policy
        dimension, so the grant has to say."""
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        push = by_name["cli.repository.push"]
        assert push.required_grant_scope_keys == ("connection_id", "repository", "branch")
        assert set(push.required_grant_scope_keys) <= set(push.scope_keys)

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
        assert by_name["cli.repository.push"].scope_keys == (
            "connection_id",
            "repository",
            "branch",
        )
        assert "command" in by_name["cli.test.run"].scope_keys
        for name in (
            "cli.file.read",
            "cli.file.write",
            "cli.file.edit",
            "cli.file.list",
            "cli.file.search",
        ):
            assert "path" in by_name[name].scope_keys

    def test_test_run_declares_no_network_dimension_because_it_has_none(self) -> None:
        by_name = {definition.name: definition for definition, _ in CLI_TOOLS}
        assert "network" not in by_name["cli.test.run"].scope_keys
        assert "network" not in by_name["cli.test.run"].input_model.model_fields

    def test_manifest_declares_the_repository_allow_list(self) -> None:
        assert CLI_MANIFEST.auth_schemes[0].type == "none"
        assert CLI_MANIFEST.auth_schemes[0].secret_fields == ()
        assert not CLI_MANIFEST.supports_webhooks
        field = next(f for f in CLI_MANIFEST.config_fields if f.name == "allowed_repositories")
        assert field.kind == "string_list"
        assert field.required is False  # absent means deny, not "must be set to save"


class TestInputValidation:
    def test_absolute_and_traversal_paths_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FileReadInput(connection_id="c", path="/etc/passwd")
        with pytest.raises(ValidationError):
            FileReadInput(connection_id="c", path="../outside")
        with pytest.raises(ValidationError):
            FileWriteInput(connection_id="c", path="a/../../b", content="x", read_token="")
        assert FileReadInput(connection_id="c", path="src/app.py").path == "src/app.py"

    @pytest.mark.parametrize("path", GIT_INTERNAL_PATHS)
    def test_path_validator_refuses_git_internals(self, path: str) -> None:
        """Hole 1. ``.git/config`` used to be a legal path under the shipped
        preset's ``path: "*"`` grant, which made a credential helper and an
        ``insteadOf`` redirect one WRITE call away."""
        with pytest.raises(ValidationError):
            FileReadInput(connection_id="c", path=path)
        with pytest.raises(ValidationError):
            FileWriteInput(connection_id="c", path=path, content="x", read_token="")
        with pytest.raises(ValidationError):
            FileEditInput(connection_id="c", path=path, old_string="a", new_string="b")
        with pytest.raises(ValidationError):
            FileListInput(connection_id="c", path=path)
        with pytest.raises(ValidationError):
            FileSearchInput(connection_id="c", pattern="x", path=path)

    @pytest.mark.parametrize("path", GIT_ADJACENT_PATHS)
    def test_path_validator_allows_ordinary_dot_git_content(self, path: str) -> None:
        assert FileReadInput(connection_id="c", path=path).path == path
        assert FileWriteInput(connection_id="c", path=path, content="x", read_token="").path == path

    def test_list_and_search_accept_an_empty_path_meaning_the_whole_tree(self) -> None:
        assert FileListInput(connection_id="c").path == ""
        assert FileSearchInput(connection_id="c", pattern="x").path == ""

    def test_a_glob_matches_one_segment(self) -> None:
        with pytest.raises(ValidationError):
            FileListInput(connection_id="c", glob="src/*.py")
        assert FileListInput(connection_id="c", glob="*.py").glob == "*.py"

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

    def test_push_branch_shape_enforced(self) -> None:
        for branch in ("", "a branch", "agent/x;rm -rf /", "--force", "-f", ".hidden"):
            with pytest.raises(ValidationError):
                RepositoryPushInput(
                    connection_id="c",
                    repository="octo/alpha",
                    branch=branch,
                    commit_message="m",
                )
        ok = RepositoryPushInput(
            connection_id="c", repository="octo/alpha", branch="agent/fix-1", commit_message="m"
        )
        assert ok.branch == "agent/fix-1"

    def test_push_takes_no_image_so_no_call_chooses_where_the_token_runs(self) -> None:
        with pytest.raises(ValidationError):
            RepositoryPushInput(
                connection_id="c",
                repository="octo/alpha",
                branch="agent/fix",
                commit_message="m",
                image="attacker/image",  # type: ignore[call-arg]
            )

    def test_file_write_requires_a_read_token(self) -> None:
        with pytest.raises(ValidationError):
            FileWriteInput(connection_id="c", path="a.py", content="x")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            FileWriteInput(connection_id="c", path="a.py", content="x", read_token="not-a-sha")
        assert FileWriteInput(connection_id="c", path="a.py", content="x", read_token="").path

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CommandExecuteInput(connection_id="c", command="ls", host_mount="/")  # type: ignore[call-arg]
