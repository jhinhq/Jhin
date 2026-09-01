"""The outbound target policy, and the leaf property that lets it live here."""

import ast
import sys
from pathlib import Path

import pytest

from jhin_domain.endpoints import (
    EndpointPolicyError,
    validate_http_origin,
    validate_public_http_url,
)

MODULE = Path(__file__).resolve().parents[1] / "src" / "jhin_domain" / "endpoints.py"


def test_the_policy_module_imports_nothing_beyond_the_standard_library() -> None:
    """What keeps this package installable everywhere that needs the policy.

    ``jhin_domain`` is the one package every other may depend on, so a single
    third-party import added here is inherited by the API, all five workers,
    and every package between them. It is also the property that let the policy
    move down out of ``jhin_connectors`` in the first place.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
    roots = {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert roots <= sys.stdlib_module_names | {"__future__"}, sorted(
        roots - sys.stdlib_module_names
    )


def test_a_public_https_origin_is_allowed_and_a_private_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The resolver branch belongs to the connector suite; this one stays offline.
    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "1")
    assert validate_public_http_url("https://api.example.com/v1") == "https://api.example.com/v1"
    with pytest.raises(EndpointPolicyError):
        validate_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_an_official_origin_is_matched_exactly_and_nothing_else_is() -> None:
    official = ("https://api.example.com",)
    assert validate_http_origin("https://api.example.com", official_origins=official) == (
        "https://api.example.com"
    )
    with pytest.raises(EndpointPolicyError):
        validate_http_origin("https://api.example.com.evil.test", official_origins=official)
