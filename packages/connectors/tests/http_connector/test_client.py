"""URL policy, path joining (SSRF guards), header hygiene, and auth headers."""

from __future__ import annotations

import base64

import pytest

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.http.client import (
    auth_headers,
    default_headers_from_config,
    join_url,
    request_headers,
    validate_http_base_url,
)

# --- base URL policy (shared with MCP) ---


def test_public_https_base_url_is_allowed_and_normalized() -> None:
    assert validate_http_base_url("https://API.Example.com/v1") == "https://api.example.com/v1"


@pytest.mark.parametrize(
    "raw",
    [
        "http://api.example.com",  # plain http, not allow-listed
        "https://localhost:8443",
        "https://10.0.0.8",
        "https://192.168.1.10/api",
        "https://user:secret-marker@api.example.com",
        "ftp://api.example.com",
        "https://api.example.com/#frag",
        "",
    ],
)
def test_disallowed_base_urls_are_rejected_without_echoing_secrets(raw: str) -> None:
    with pytest.raises(EndpointPolicyError) as exc_info:
        validate_http_base_url(raw)
    assert "secret-marker" not in str(exc_info.value)


def test_operator_allowlist_admits_in_stack_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-github:8080")
    assert validate_http_base_url("http://fake-github:8080") == "http://fake-github:8080/"
    with pytest.raises(EndpointPolicyError):
        validate_http_base_url("http://other-host:8080")


# --- path joining ---


def test_paths_join_under_the_base_url() -> None:
    assert join_url("https://api.example.com/", "/v1/items") == "https://api.example.com/v1/items"
    assert join_url("https://api.example.com/v1", "items/") == "https://api.example.com/v1/items/"
    assert join_url("https://api.example.com/v1/", "") == "https://api.example.com/v1/"
    assert join_url("https://api.example.com/", "a//b") == "https://api.example.com/a/b"


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/v1/../secrets",
        "..",
        "/a/b/../../../etc",
        "/v1?x=1",
        "/v1#frag",
        "/v1/\x00",
        "/v1/ space",
        "back\\slash",
    ],
)
def test_escaping_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        join_url("https://api.example.com/v1", path)


# --- header hygiene ---


def test_request_headers_reject_auth_cookie_and_transport_headers() -> None:
    assert request_headers({"X-Request-Id": "abc"}) == {"X-Request-Id": "abc"}
    forbidden_names = (
        "Authorization",
        "Cookie",
        "Host",
        "Transfer-Encoding",
        "proxy-authorization",
    )
    for forbidden in forbidden_names:
        with pytest.raises(ValueError, match="reserved"):
            request_headers({forbidden: "x"})
    with pytest.raises(ValueError):
        request_headers({"X-Bad": "multi\r\nline"})
    with pytest.raises(ValueError):
        request_headers({"bad header": "x"})


def test_default_headers_config_parses_name_value_lines() -> None:
    parsed = default_headers_from_config(
        {"default_headers": ["Accept: application/vnd.api+json", "X-Env: dev"]}
    )
    assert parsed == {"Accept": "application/vnd.api+json", "X-Env": "dev"}
    assert default_headers_from_config({}) == {}
    with pytest.raises(ValueError, match="Name: value"):
        default_headers_from_config({"default_headers": ["no-separator"]})
    with pytest.raises(ValueError, match="reserved"):
        default_headers_from_config({"default_headers": ["Authorization: Bearer leak"]})


# --- auth schemes ---


def test_auth_headers_for_each_scheme() -> None:
    assert auth_headers("none", {}, {}) == {}
    assert auth_headers("bearer", {"token": "tok"}, {}) == {"Authorization": "Bearer tok"}
    assert auth_headers("header", {"token": "tok"}, {"header_name": "X-API-Key"}) == {
        "X-API-Key": "tok"
    }
    encoded = base64.b64encode(b"user:pw").decode()
    assert auth_headers("basic", {"username": "user", "password": "pw"}, {}) == {
        "Authorization": f"Basic {encoded}"
    }


def test_auth_headers_reject_missing_or_malformed_material() -> None:
    with pytest.raises(ValueError, match="stores no token"):
        auth_headers("bearer", {}, {})
    with pytest.raises(ValueError, match="malformed"):
        auth_headers("bearer", {"token": "bad\r\ntoken"}, {})
    with pytest.raises(ValueError, match="basic-auth"):
        auth_headers("basic", {"username": "user"}, {})
    with pytest.raises(ValueError, match="malformed"):
        auth_headers("basic", {"username": "u:ser", "password": "pw"}, {})
    with pytest.raises(ValueError, match="reserved"):
        auth_headers("header", {"token": "tok"}, {"header_name": "Authorization"})
    with pytest.raises(ValueError, match="unsupported"):
        auth_headers("oauth", {"token": "tok"}, {})
