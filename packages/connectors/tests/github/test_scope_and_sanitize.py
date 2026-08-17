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
