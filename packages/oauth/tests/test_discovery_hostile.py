"""Discovery treated as what it is: parsing an attacker's document.

Everything in an authorization-server metadata document arrives from a URL the
server being connected to chose. This file is the adversarial half of
``test_discovery.py``: each test is one thing a hostile or merely broken server
can put in that document, and the assertion is that Jhin refuses rather than
proceeds.

Two refusals are load-bearing and deliberately absolute:

- an issuer that does not byte-match **aborts the flow** and does not fall
  through to the next candidate URL — a mismatch is an active mix-up signal,
  not a 404;
- **no ``S256`` means no flow.** Absent, or present without ``S256``, both
  raise. There is no setting that turns this off.
"""

from __future__ import annotations

import httpx
import pytest
from packages.oauth.tests.conftest import StartServer

from jhin_connectors.testing.fake_oauth import FakeAsConfig
from jhin_oauth.discovery import (
    MAX_AUTHORIZATION_SERVERS,
    MAX_SCOPE_ENTRIES,
    discover_authorization_server,
    discover_protected_resource,
    parse_authorization_server_metadata,
    parse_protected_resource_metadata,
    probe_mcp_endpoint,
)
from jhin_oauth.errors import DiscoveryError, IssuerMismatchError, PkceUnsupportedError

ISSUER = "https://as.example.com"


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "code_challenge_methods_supported": ["S256"],
    }
    document.update(overrides)
    return document


# --- issuer identity -------------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        f"{ISSUER}/",
        "https://AS.example.com",
        "https://as.example.com:443",
        "https://as.example.com.evil.test",
    ],
)
def test_issuer_comparison_is_byte_identical_with_no_normalization(expected: str) -> None:
    # Every one of these is "the same server" under some normalization rule.
    # RFC 8414 §3.3 says none of those rules apply, and every one of them is a
    # way for two different servers to look like one.
    with pytest.raises(IssuerMismatchError):
        parse_authorization_server_metadata(_document(), expected_issuer=expected)


def test_a_missing_issuer_is_a_mismatch_not_a_default() -> None:
    document = _document()
    del document["issuer"]
    with pytest.raises(IssuerMismatchError):
        parse_authorization_server_metadata(document, expected_issuer=ISSUER)


async def test_an_issuer_mismatch_aborts_instead_of_trying_the_next_candidate(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(
        FakeAsConfig(metadata_style="both", metadata_issuer_override="https://evil.example.com")
    )
    with pytest.raises(IssuerMismatchError):
        await discover_authorization_server(http_client, server.issuer)

    attempted = [
        record["path"]
        for record in server.recorded_requests()
        if ".well-known" in str(record["path"])
    ]
    assert len(attempted) == 1, "a mismatch must abort, not walk the rest of the ladder"


async def test_a_probe_reports_an_issuer_mismatch_without_raising(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(metadata_issuer_override="https://evil.example.com"))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert probe.failure_reason == "issuer_mismatch"
    assert not probe.supports_oauth
    assert probe.authorization_server is None


# --- PKCE ------------------------------------------------------------------


def test_absent_code_challenge_methods_is_left_for_discovery_to_refuse() -> None:
    document = _document()
    del document["code_challenge_methods_supported"]
    # The parser still builds the document; the refusal is the discovery step's,
    # because a synthesized static provider legitimately has no such field.
    metadata = parse_authorization_server_metadata(document, expected_issuer=ISSUER)
    assert metadata.code_challenge_methods_supported == ()


@pytest.mark.parametrize("methods", [(), ("plain",)])
async def test_a_server_without_s256_is_refused(
    http_client: httpx.AsyncClient, start_server: StartServer, methods: tuple[str, ...]
) -> None:
    server = start_server(FakeAsConfig(code_challenge_methods=methods))
    with pytest.raises(PkceUnsupportedError):
        await discover_authorization_server(http_client, server.issuer)


async def test_a_probe_of_a_no_s256_server_reports_it_and_offers_no_oauth(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(code_challenge_methods=("plain",)))
    probe = await probe_mcp_endpoint(http_client, server.mcp_url)
    assert probe.failure_reason == "pkce_unsupported"
    assert not probe.supports_oauth


# --- transport-level hostility ---------------------------------------------


async def test_oversized_metadata_is_refused(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(oversized_metadata=True))
    with pytest.raises(DiscoveryError):
        await discover_authorization_server(http_client, server.issuer)


async def test_a_redirecting_metadata_endpoint_is_refused(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    # Following it would launder the SSRF policy's refusal into a fetch.
    server = start_server(FakeAsConfig(redirect_on_metadata=True))
    with pytest.raises(DiscoveryError):
        await discover_authorization_server(http_client, server.issuer)


# --- SSRF at parse time ----------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/token",
        "https://169.254.169.254/token",
        "https://127.0.0.1/token",
        "https://10.0.0.1/token",
        "https://192.168.1.1/token",
        "https://[::1]/token",
        "https://[::ffff:169.254.169.254]/token",
        "https://2130706433/token",
        "http://as.example.com/token",
        "https://user:pass@as.example.com/token",
        "https://as.example.com/token#fragment",
    ],
)
def test_a_hostile_token_endpoint_is_refused_at_parse_time(endpoint: str) -> None:
    with pytest.raises(DiscoveryError):
        parse_authorization_server_metadata(
            _document(token_endpoint=endpoint), expected_issuer=ISSUER
        )


def test_a_hostile_authorization_endpoint_is_refused_at_parse_time() -> None:
    with pytest.raises(DiscoveryError):
        parse_authorization_server_metadata(
            _document(authorization_endpoint="http://169.254.169.254/authorize"),
            expected_issuer=ISSUER,
        )


def test_a_hostile_optional_endpoint_is_dropped_rather_than_fatal() -> None:
    # An unusable registration endpoint should degrade the connection to "no
    # dynamic registration", not kill it.
    metadata = parse_authorization_server_metadata(
        _document(
            registration_endpoint="http://169.254.169.254/register",
            revocation_endpoint="https://as.example.com/revoke",
        ),
        expected_issuer=ISSUER,
    )
    assert metadata.registration_endpoint is None
    assert not metadata.supports_dcr()
    assert metadata.revocation_endpoint == "https://as.example.com/revoke"


# --- shape and bounds ------------------------------------------------------


@pytest.mark.parametrize("document", ["a string", 7, None, [], True])
def test_a_metadata_document_that_is_not_an_object_is_refused(document: object) -> None:
    with pytest.raises(DiscoveryError):
        parse_authorization_server_metadata(document, expected_issuer=ISSUER)


@pytest.mark.parametrize("document", ["a string", 7, None, []])
def test_a_protected_resource_document_that_is_not_an_object_is_refused(
    document: object,
) -> None:
    with pytest.raises(DiscoveryError):
        parse_protected_resource_metadata(document, source_url="https://x.example/prm")


def test_protected_resource_authorization_servers_are_truncated_to_sixteen() -> None:
    metadata = parse_protected_resource_metadata(
        {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": [f"https://as{index}.example.com" for index in range(100)],
        },
        source_url="https://mcp.example.com/prm",
    )
    assert len(metadata.authorization_servers) == MAX_AUTHORIZATION_SERVERS


def test_string_arrays_are_truncated_and_filtered() -> None:
    metadata = parse_authorization_server_metadata(
        _document(
            scopes_supported=["read", 7, None, {"a": 1}, "write"]
            + [f"s{index}" for index in range(300)],
            grant_types_supported=["authorization_code", 5],
            token_endpoint_auth_methods_supported=["none"],
        ),
        expected_issuer=ISSUER,
    )
    assert len(metadata.scopes_supported) <= MAX_SCOPE_ENTRIES
    assert 7 not in metadata.scopes_supported
    assert metadata.grant_types_supported == ("authorization_code",)


def test_an_over_long_scope_entry_is_dropped() -> None:
    metadata = parse_authorization_server_metadata(
        _document(scopes_supported=["read", "x" * 500]), expected_issuer=ISSUER
    )
    assert metadata.scopes_supported == ("read",)


@pytest.mark.parametrize("value", ["true", 1, "yes", [], {}, None])
def test_only_a_real_json_true_is_true(value: object) -> None:
    metadata = parse_authorization_server_metadata(
        _document(authorization_response_iss_parameter_supported=value),
        expected_issuer=ISSUER,
    )
    assert metadata.authorization_response_iss_parameter_supported is False


def test_a_real_json_true_is_true() -> None:
    metadata = parse_authorization_server_metadata(
        _document(
            authorization_response_iss_parameter_supported=True,
            client_id_metadata_document_supported=True,
        ),
        expected_issuer=ISSUER,
    )
    assert metadata.authorization_response_iss_parameter_supported is True
    assert metadata.client_id_metadata_document_supported is True


def test_unknown_fields_are_ignored_rather_than_splatted() -> None:
    metadata = parse_authorization_server_metadata(
        _document(some_future_field={"nested": True}, __class__="evil"),
        expected_issuer=ISSUER,
    )
    assert metadata.issuer == ISSUER


def test_a_protected_resource_naming_a_foreign_origin_is_still_parsed_but_not_trusted() -> None:
    # Parsing is not trusting: discover_protected_resource is what refuses a
    # document whose resource does not cover the endpoint being probed.
    metadata = parse_protected_resource_metadata(
        {"resource": "https://someone-else.example.com", "authorization_servers": []},
        source_url="https://mcp.example.com/prm",
    )
    assert metadata.resource == "https://someone-else.example.com"


async def test_a_protected_resource_claiming_another_origin_is_discarded(
    http_client: httpx.AsyncClient, start_server: StartServer
) -> None:
    server = start_server(FakeAsConfig(prm_resource_override="https://someone-else.example.com"))
    with pytest.raises(DiscoveryError):
        await discover_protected_resource(http_client, server.mcp_url)


def test_a_resource_identifier_with_a_fragment_is_refused() -> None:
    with pytest.raises(DiscoveryError):
        parse_protected_resource_metadata(
            {"resource": "https://mcp.example.com/mcp#frag"}, source_url="https://x/prm"
        )


def test_an_over_long_resource_identifier_is_refused() -> None:
    with pytest.raises(DiscoveryError):
        parse_protected_resource_metadata(
            {"resource": "https://mcp.example.com/" + "a" * 2000},
            source_url="https://x/prm",
        )


# --- nothing leaks ---------------------------------------------------------


def test_no_refusal_message_carries_provider_text() -> None:
    provider_prose = "CONTACT-YOUR-ADMINISTRATOR-AT-EVIL-DOT-COM"
    with pytest.raises(DiscoveryError) as caught:
        parse_authorization_server_metadata(
            _document(token_endpoint=f"http://evil.example/{provider_prose}"),
            expected_issuer=ISSUER,
        )
    assert provider_prose not in str(caught.value)

    with pytest.raises(IssuerMismatchError) as mismatch:
        parse_authorization_server_metadata(
            _document(issuer=f"https://{provider_prose}.example.com"),
            expected_issuer=ISSUER,
        )
    assert provider_prose not in str(mismatch.value)
