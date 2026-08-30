"""The discovery chain, end to end against a real authorization server.

Each test configures the fake to behave like one shape of deployment seen in
the wild — a root-only protected-resource document, a path-inserted one, an
issuer with a path, OpenID-only metadata, no protected-resource document at all
— and asserts that the probe reaches the same answer a browser would.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from packages.oauth.tests.conftest import StartServer

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer
from jhin_oauth.discovery import (
    discover_authorization_server,
    discover_protected_resource,
    parse_protected_resource_metadata,
    probe_mcp_endpoint,
    select_scopes,
)
from jhin_oauth.errors import DiscoveryError


async def test_probe_walks_the_challenge_all_the_way_to_metadata(
    fake_as: FakeAuthorizationServer, http_client: httpx.AsyncClient
) -> None:
    probe = await probe_mcp_endpoint(http_client, fake_as.mcp_url)

    assert probe.requires_auth
    assert probe.failure_reason is None
    assert probe.supports_oauth
    assert probe.supports_dcr
    assert probe.challenge_scope == "read"
    assert probe.resource_metadata_url is not None
    assert probe.resource_metadata_url.endswith("/.well-known/oauth-protected-resource/mcp")
    assert probe.protected_resource is not None
    assert probe.protected_resource.resource == fake_as.resource
    assert probe.authorization_server is not None
    assert probe.authorization_server.issuer == fake_as.issuer
    assert "S256" in probe.authorization_server.code_challenge_methods_supported


async def test_probe_reports_a_server_that_needs_no_authentication(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(require_auth=False))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert not probe.requires_auth
    assert not probe.supports_oauth
    assert probe.failure_reason == "no_authentication_required"


async def test_root_only_protected_resource_metadata_is_found(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(prm_style="root"))
    metadata = await discover_protected_resource(http_client, server.mcp_url)
    assert metadata.source_url.endswith("/.well-known/oauth-protected-resource")
    assert metadata.authorization_servers == (server.issuer,)


async def test_path_inserted_protected_resource_metadata_is_found(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(prm_style="path_inserted"))
    metadata = await discover_protected_resource(http_client, server.mcp_url)
    assert metadata.source_url.endswith("/.well-known/oauth-protected-resource/mcp")


async def test_the_challenge_url_is_preferred_over_the_well_known_ladder(
    fake_as: FakeAuthorizationServer, http_client: httpx.AsyncClient
) -> None:
    await discover_protected_resource(
        http_client,
        fake_as.mcp_url,
        resource_metadata_url=f"{fake_as.base_url}/.well-known/oauth-protected-resource",
    )
    metadata_requests = fake_as.recorded_requests(path_suffix="oauth-protected-resource")
    assert metadata_requests, "the challenge-supplied URL was never fetched"


async def test_no_protected_resource_metadata_falls_back_to_the_origin(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    # Atlassian-shaped: no RFC 9728 document, an authorization server at the
    # origin root.
    server = start_server(FakeAsConfig(prm_style="none"))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert probe.protected_resource is None
    assert probe.supports_oauth
    assert probe.authorization_server is not None
    assert probe.authorization_server.issuer == server.issuer


async def test_discover_protected_resource_raises_when_nothing_resolves(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(prm_style="none"))
    with pytest.raises(DiscoveryError):
        await discover_protected_resource(http_client, server.mcp_url)


async def test_issuer_with_a_path_walks_the_three_candidate_ladder(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    # metadata_style="openid" serves only the third candidate (path appending),
    # so reaching it proves the first two were tried and skipped.
    server = start_server(FakeAsConfig(issuer_path="/tenant1", metadata_style="openid"))
    metadata = await discover_authorization_server(http_client, server.issuer)
    assert metadata.issuer == server.issuer

    attempted = [
        record["path"]
        for record in server.recorded_requests()
        if ".well-known" in str(record["path"])
    ]
    assert attempted[:3] == [
        "/.well-known/oauth-authorization-server/tenant1",
        "/.well-known/openid-configuration/tenant1",
        "/tenant1/.well-known/openid-configuration",
    ]


async def test_openid_only_metadata_is_accepted_for_a_bare_issuer(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(metadata_style="openid"))
    metadata = await discover_authorization_server(http_client, server.issuer)
    assert metadata.token_endpoint == f"{server.base_url}/token"


async def test_metadata_optional_endpoints_are_captured(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(supports_device_flow=True))
    metadata = await discover_authorization_server(http_client, server.issuer)
    assert metadata.registration_endpoint == f"{server.base_url}/register"
    assert metadata.revocation_endpoint == f"{server.base_url}/revoke"
    assert metadata.device_authorization_endpoint == f"{server.base_url}/device/code"
    assert metadata.authorization_response_iss_parameter_supported is True
    assert metadata.supports_dcr()
    assert metadata.supports_refresh()


async def test_a_server_without_registration_reports_no_dcr(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(supports_dcr=False))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert probe.supports_oauth
    assert not probe.supports_dcr


async def test_an_unquoted_challenge_is_parsed(
    start_server: StartServer, http_client: httpx.AsyncClient
) -> None:
    server = start_server(FakeAsConfig(challenge_quoted=False))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert probe.resource_metadata_url is not None
    assert probe.supports_oauth


async def test_an_unreachable_server_is_reported_not_raised(
    http_client: httpx.AsyncClient, allow_origins: Callable[[str], None]
) -> None:
    allow_origins("http://127.0.0.1:9")
    probe = await probe_mcp_endpoint(http_client, "http://127.0.0.1:9/mcp")
    assert probe.failure_reason == "unreachable"
    assert not probe.supports_oauth


def test_protected_resource_metadata_bounds_its_arrays() -> None:
    metadata = parse_protected_resource_metadata(
        {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": [f"https://as{index}.example.com" for index in range(100)],
            "scopes_supported": ["read", 7, None, "write"],
        },
        source_url="https://mcp.example.com/.well-known/oauth-protected-resource",
    )
    assert len(metadata.authorization_servers) == 16
    assert metadata.scopes_supported == ("read", "write")


def test_select_scopes_prefers_the_challenge_scope() -> None:
    assert (
        select_scopes(
            challenge_scope="issues:read issues:write",
            resource_scopes=["everything"],
            server_scopes=["issues:read", "issues:write"],
            want_offline_access=False,
        )
        == "issues:read issues:write"
    )


def test_select_scopes_falls_back_to_the_resource_then_to_nothing() -> None:
    assert (
        select_scopes(
            challenge_scope=None,
            resource_scopes=["read", "write", "read"],
            server_scopes=[],
            want_offline_access=False,
        )
        == "read write"
    )
    assert (
        select_scopes(
            challenge_scope="  ",
            resource_scopes=[],
            server_scopes=["read"],
            want_offline_access=False,
        )
        == ""
    )


def test_select_scopes_adds_offline_access_only_when_advertised() -> None:
    assert (
        select_scopes(
            challenge_scope="read",
            resource_scopes=[],
            server_scopes=["read", "offline_access"],
            want_offline_access=True,
        )
        == "read offline_access"
    )
    assert (
        select_scopes(
            challenge_scope="read",
            resource_scopes=[],
            server_scopes=["read"],
            want_offline_access=True,
        )
        == "read"
    )


def test_select_scopes_drops_wildcards() -> None:
    assert (
        select_scopes(
            challenge_scope=None,
            resource_scopes=["*", "all", "full-access", "read"],
            server_scopes=[],
            want_offline_access=False,
        )
        == "read"
    )


def test_select_scopes_bounds_the_rendered_string() -> None:
    rendered = select_scopes(
        challenge_scope=None,
        resource_scopes=[f"scope-{index:03d}" for index in range(200)],
        server_scopes=[],
        want_offline_access=False,
    )
    assert len(rendered) <= 2048
    assert len(rendered.split()) <= 64
