"""URL policy: canonical audiences, discovery ladders, challenge parsing."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_oauth.urls import (
    canonical_resource_uri,
    parse_www_authenticate,
    same_origin,
    validate_oauth_url,
    well_known_as_candidates,
    well_known_prm_candidates,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://mcp.example.com/mcp", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com", "https://mcp.example.com"),
        ("https://mcp.example.com/", "https://mcp.example.com"),
        ("https://mcp.example.com:8443", "https://mcp.example.com:8443"),
        ("https://mcp.example.com:443", "https://mcp.example.com"),
        ("https://mcp.example.com/server/mcp", "https://mcp.example.com/server/mcp"),
        ("https://mcp.example.com/server/mcp/", "https://mcp.example.com/server/mcp"),
        ("HTTPS://MCP.Example.COM/MCP", "https://mcp.example.com/MCP"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://localhost:80/mcp", "http://localhost/mcp"),
    ],
)
def test_canonical_resource_uri_matches_the_spec_table(raw: str, expected: str) -> None:
    assert canonical_resource_uri(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "mcp.example.com",
        "https://mcp.example.com#fragment",
        "https://mcp.example.com/mcp#anchor",
        "",
        "   ",
        "ftp://mcp.example.com",
    ],
)
def test_canonical_resource_uri_refuses_relative_and_fragmented(raw: str) -> None:
    with pytest.raises(ValueError):
        canonical_resource_uri(raw)


def test_canonical_resource_uri_lowercases_only_the_authority() -> None:
    # Paths are case-sensitive; hosts are not. Folding both would merge two
    # different resources into one audience.
    assert canonical_resource_uri("https://Example.COM/Tenant/MCP") == (
        "https://example.com/Tenant/MCP"
    )


def test_same_origin_ignores_the_path() -> None:
    assert same_origin("https://example.com", "https://example.com/mcp")
    assert not same_origin("https://example.com", "https://other.example.com/mcp")
    assert not same_origin("https://example.com", "http://example.com")


def test_prm_candidates_put_path_insertion_first() -> None:
    assert well_known_prm_candidates("https://mcp.example.com/tenant/mcp") == (
        "https://mcp.example.com/.well-known/oauth-protected-resource/tenant/mcp",
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    )


def test_prm_candidates_omit_path_insertion_for_a_root_endpoint() -> None:
    assert well_known_prm_candidates("https://mcp.example.com") == (
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    )


def test_as_candidates_for_an_issuer_with_a_path_are_three_in_order() -> None:
    assert well_known_as_candidates("https://auth.example.com/tenant1") == (
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
        "https://auth.example.com/.well-known/openid-configuration/tenant1",
        "https://auth.example.com/tenant1/.well-known/openid-configuration",
    )


def test_as_candidates_for_a_bare_issuer_are_two_in_order() -> None:
    assert well_known_as_candidates("https://auth.example.com") == (
        "https://auth.example.com/.well-known/oauth-authorization-server",
        "https://auth.example.com/.well-known/openid-configuration",
    )


def test_as_candidates_refuse_an_issuer_carrying_a_query() -> None:
    # RFC 8414 §2 forbids it, and every candidate would silently drop the part
    # that made the issuer distinct.
    with pytest.raises(EndpointPolicyError):
        well_known_as_candidates("https://auth.example.com/tenant?realm=a")


@pytest.mark.parametrize(
    "raw",
    [
        "http://public.example.com/token",
        "https://user:pass@example.com/token",
        "https://example.com/token#fragment",
        "https://169.254.169.254/token",
        "https://127.0.0.1/token",
        "https://10.0.0.1/token",
        "https://[::1]/token",
        "https://[::ffff:127.0.0.1]/token",
        "https://2130706433/token",
        "not-a-url",
        "",
    ],
)
def test_validate_oauth_url_refuses_what_the_policy_refuses(raw: str) -> None:
    with pytest.raises(EndpointPolicyError):
        validate_oauth_url(raw, kind="token endpoint")


def test_validate_oauth_url_refuses_an_over_long_url() -> None:
    with pytest.raises(EndpointPolicyError):
        validate_oauth_url("https://example.com/" + "a" * 3000, kind="token endpoint")


def test_validate_oauth_url_allows_plaintext_only_for_an_allow_listed_origin(
    allow_origins: Callable[[str], None],
) -> None:
    allow_origins("http://127.0.0.1:9911")
    assert (
        validate_oauth_url("http://127.0.0.1:9911/token", kind="token endpoint")
        == "http://127.0.0.1:9911/token"
    )
    with pytest.raises(EndpointPolicyError):
        validate_oauth_url("http://127.0.0.1:9912/token", kind="token endpoint")


def test_parse_www_authenticate_reads_quoted_params() -> None:
    header = (
        'Bearer realm="mcp", error="invalid_token", '
        'resource_metadata="https://example.com/.well-known/oauth-protected-resource", '
        'scope="read write"'
    )
    assert parse_www_authenticate(header) == {
        "realm": "mcp",
        "error": "invalid_token",
        "resource_metadata": ("https://example.com/.well-known/oauth-protected-resource"),
        "scope": "read write",
    }


def test_parse_www_authenticate_reads_unquoted_params() -> None:
    # At least one large provider emits these unquoted; RFC 9110 allows it.
    header = "Bearer realm=example, error=invalid_token"
    assert parse_www_authenticate(header) == {"realm": "example", "error": "invalid_token"}


def test_parse_www_authenticate_unescapes_quoted_pairs() -> None:
    assert parse_www_authenticate(r'Bearer realm="a\"b"') == {"realm": 'a"b'}


@pytest.mark.parametrize(
    "header",
    ["", 'Basic realm="x"', "gibberish", "Bearer", "Bearer ,,,", "x" * 9000],
)
def test_parse_www_authenticate_never_raises(header: str) -> None:
    assert parse_www_authenticate(header) == {}


def test_parse_www_authenticate_bounds_the_parameter_count() -> None:
    header = "Bearer " + ", ".join(f"p{index}=v{index}" for index in range(50))
    assert len(parse_www_authenticate(header)) == 16


def test_parse_www_authenticate_drops_oversized_values() -> None:
    header = f'Bearer realm="ok", scope="{"s" * 3000}"'
    parsed = parse_www_authenticate(header)
    assert parsed == {"realm": "ok"}
