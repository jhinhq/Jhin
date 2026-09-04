"""Connection/repo/branch scope matching through the policy evaluator, and
token redaction in sanitized error payloads (plan 6.6, 12, 48.9)."""

from jhin_connectors.registry import build_default_catalog
from jhin_policy import Grant, GrantEffect, evaluate, scope_matches
from jhin_secrets.redaction import SecretRedactor
from jhin_tools.sanitize import sanitize_payload

CONNECTION = "0198c5f2-0000-7000-8000-000000000001"


def _tool(name: str):  # type: ignore[no-untyped-def]
    definition = build_default_catalog().registry.get(name)
    assert definition is not None
    return definition


def test_repo_glob_scope_allows_matching_repo() -> None:
    grants = [
        Grant(
            capability="github.repository.read",
            scope={"connection_id": CONNECTION, "repository": "octo/*"},
            effect=GrantEffect.ALLOW,
        )
    ]
    decision = evaluate(
        _tool("github.repository.read"),
        grants=grants,
        rules=[],
        requested_scope={"connection_id": CONNECTION, "repository": "octo/alpha"},
    )
    assert decision.decision.value == "allow"


def test_repo_scope_mismatch_denied() -> None:
    grants = [
        Grant(
            capability="github.repository.read",
            scope={"connection_id": CONNECTION, "repository": "octo/alpha"},
            effect=GrantEffect.ALLOW,
        )
    ]
    decision = evaluate(
        _tool("github.repository.read"),
        grants=grants,
        rules=[],
        requested_scope={"connection_id": CONNECTION, "repository": "octo/beta"},
    )
    assert decision.decision.value == "deny"
    assert decision.code == "scope_mismatch"


def test_different_connection_denied() -> None:
    grants = [
        Grant(
            capability="github.repository.read",
            scope={"connection_id": CONNECTION},
            effect=GrantEffect.ALLOW,
        )
    ]
    decision = evaluate(
        _tool("github.repository.read"),
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": "0198c5f2-0000-7000-8000-00000000dead",
            "repository": "octo/alpha",
        },
    )
    assert decision.code == "scope_mismatch"


def test_branch_glob_scope() -> None:
    grants = [
        Grant(
            capability="github.branch.create",
            scope={"connection_id": CONNECTION, "branch": "agent/*"},
            effect=GrantEffect.ALLOW,
        )
    ]
    tool = _tool("github.branch.create")
    allowed = evaluate(
        tool,
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "branch": "agent/fix-login",
        },
    )
    assert allowed.decision.value == "allow"
    denied = evaluate(
        tool,
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "branch": "main",
        },
    )
    assert denied.code == "scope_mismatch"


def test_repo_list_scope_means_any_of() -> None:
    assert scope_matches({"repository": ["octo/alpha", "octo/beta"]}, {"repository": "octo/beta"})
    assert not scope_matches(
        {"repository": ["octo/alpha", "octo/beta"]}, {"repository": "octo/gamma"}
    )


def test_sanitizer_strips_registered_tokens_from_error_bodies() -> None:
    redactor = SecretRedactor()
    redactor.register("ghp_supersecrettoken12345")
    redactor.register("ghs_installation_token_abcdef")
    hostile_error = {
        "error": (
            "GitHubApiError: GitHub API POST /repos/octo/alpha/pulls failed (401): "
            "Bad credentials for token ghp_supersecrettoken12345"
        ),
        "nested": {"echo": "Authorization: Bearer ghs_installation_token_abcdef"},
    }
    sanitized = sanitize_payload(hostile_error, redactor=redactor)
    text = str(sanitized)
    assert "ghp_supersecrettoken12345" not in text
    assert "ghs_installation_token_abcdef" not in text
    assert "[REDACTED]" in sanitized["error"]
    assert "[REDACTED]" in sanitized["nested"]["echo"]


def test_pull_request_create_scopes_head_and_base() -> None:
    """A grant that names a repository must still be able to say which base
    branch a pull request may target. Without ``base`` as a scope key the
    fnmatch never sees it, so ``repository: octo/alpha`` also authorised a
    pull request into ``production``."""
    definition = _tool("github.pull_request.create")
    assert definition.scope_keys == ("connection_id", "repository", "head", "base")
    # Required, not merely available: an unstated base is an unlimited one,
    # because scope_matches only walks the keys a grant constrains.
    assert definition.required_grant_scope_keys == ("connection_id", "repository", "base")

    grants = [
        Grant(
            capability="github.pull_request.create",
            scope={"connection_id": CONNECTION, "repository": "octo/alpha", "base": "main"},
            effect=GrantEffect.ALLOW,
        )
    ]
    into_main = evaluate(
        definition,
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "main",
        },
    )
    into_production = evaluate(
        definition,
        grants=grants,
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "production",
        },
    )
    assert into_main.decision.value == "allow"
    assert into_production.code == "scope_mismatch"


def test_pull_request_create_refuses_a_grant_that_names_no_base() -> None:
    """The hand-written grant shape the wizard never writes: a repository and
    a connection, no base. It used to authorise a pull request into any branch
    of that repository; now it is denied, and the denial names the key."""
    decision = evaluate(
        _tool("github.pull_request.create"),
        grants=[
            Grant(
                capability="github.pull_request.create",
                scope={"connection_id": CONNECTION, "repository": "octo/alpha"},
                effect=GrantEffect.ALLOW,
            )
        ],
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "production",
        },
    )
    assert decision.code == "required_scope_missing"
    assert "base" in decision.reason


def test_pull_request_create_refuses_an_unscoped_grant() -> None:
    """A bare ``github.*`` grant can no longer open pull requests anywhere."""
    decision = evaluate(
        _tool("github.pull_request.create"),
        grants=[Grant(capability="github.*", scope={}, effect=GrantEffect.ALLOW)],
        rules=[],
        requested_scope={
            "connection_id": CONNECTION,
            "repository": "octo/alpha",
            "head": "agent/fix",
            "base": "main",
        },
    )
    assert decision.code == "required_scope_missing"
