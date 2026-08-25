"""Outbound URL policy: the cases an SSRF attempt actually uses.

``validate_public_http_url`` is the only thing standing between an
agent-supplied URL (``web.fetch``, the HTTP connector, an MCP endpoint) and the
private network the self-hosted stack lives in — cloud metadata, the Docker
host gateway, Postgres, NATS, Temporal, the sandbox runner.
"""

from __future__ import annotations

import pytest

from jhin_connectors.endpoints import EndpointPolicyError, validate_public_http_url


def blocked(url: str) -> bool:
    try:
        validate_public_http_url(url)
    except EndpointPolicyError:
        return True
    return False


@pytest.mark.parametrize(
    "url",
    [
        # RFC1918
        "https://10.0.0.1/",
        "https://192.168.1.1/",
        "https://172.16.0.1/",
        # Loopback
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://localhost/",
        "https://api.localhost/",
        "https://postgres.local/",
        # Link-local, including the cloud metadata endpoint
        "https://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/latest/meta-data/",
        "https://[fe80::1]/",
        # IPv6 unique-local
        "https://[fc00::1]/",
        "https://[fd00::1]/",
        # IPv4-mapped IPv6
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:10.0.0.1]/",
        "https://[::ffff:169.254.169.254]/",
        # Unspecified, CGNAT, reserved, TEST-NET
        "https://0.0.0.0/",
        "https://100.64.0.1/",
        "https://240.0.0.1/",
        "https://192.0.2.1/",
    ],
)
def test_private_and_special_addresses_are_blocked(url: str) -> None:
    assert blocked(url)


@pytest.mark.parametrize(
    "url",
    [
        # Multicast: `is_global` alone says nothing about these.
        "https://224.0.0.1/",
        "https://239.255.255.250/",
        "https://[ff02::1]/",
    ],
)
def test_multicast_is_blocked(url: str) -> None:
    assert blocked(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # decimal 127.0.0.1
        "https://0x7f000001/",  # hex 127.0.0.1
        "https://127.1/",  # short form 127.0.0.1
        "https://010.0.0.1/",  # octal 10.0.0.1
        "https://0300.0250.0.1/",  # octal 192.168.0.1
        "https://0xa000001/",  # hex 10.0.0.1
        "https://3232235777/",  # decimal 192.168.1.1
    ],
)
def test_packed_ipv4_literals_cannot_walk_past_the_private_range_check(url: str) -> None:
    """`ipaddress` refuses these forms; `getaddrinfo` happily resolves them.

    Treating them as ordinary hostnames is how the private-range block gets
    bypassed, so they are rejected outright.
    """
    assert blocked(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/plain,hello",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_blocked(url: str) -> None:
    assert blocked(url)


def test_plain_http_to_a_public_host_needs_an_operator_allowlist_entry() -> None:
    assert blocked("http://example.com/")


def test_credentials_in_the_url_are_rejected() -> None:
    assert blocked("https://user:pass@example.com/")
    assert blocked("https://user@example.com/")


def test_fragments_are_rejected() -> None:
    assert blocked("https://example.com/path#fragment")


@pytest.mark.parametrize("url", ["https://example.com/ ", " https://example.com/", ""])
def test_malformed_urls_are_rejected(url: str) -> None:
    assert blocked(url)


def test_embedded_control_characters_are_normalised_away_not_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers use the *returned* URL, so a stripped newline cannot smuggle
    anything into the outbound request."""
    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "true")
    normalized = validate_public_http_url("https://exam\nple.com/x")
    assert normalized == "https://example.com/x"
    assert "\n" not in normalized


def test_operator_allowlisted_origin_is_accepted_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev stack's fake connectors rely on this exact escape hatch."""
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-github:8080")
    assert validate_public_http_url("http://fake-github:8080/repos") == (
        "http://fake-github:8080/repos"
    )


def test_allowlist_is_exact_and_does_not_cover_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-github:8080")
    assert blocked("http://fake-github:9090/")
    assert blocked("http://evil-fake-github:8080/")


def test_dns_name_resolving_into_private_space_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public *name* pointed at 169.254.169.254 is the standard bypass."""
    import socket

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert blocked("https://metadata.attacker.example/latest/meta-data/")


def test_dns_name_resolving_publicly_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert validate_public_http_url("https://example.com/x") == "https://example.com/x"


def test_one_private_answer_among_several_blocks_the_whole_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert blocked("https://mixed.example/")


def test_resolution_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """An air-gapped install whose resolver is down must not lose the lexical
    policy to a hard failure; a name that cannot resolve cannot be reached."""
    import socket

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        raise OSError("no resolver")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert validate_public_http_url("https://example.com/") == "https://example.com/"


def test_dns_check_can_be_disabled_by_the_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("resolver must not be consulted when disabled")

    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "true")
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert validate_public_http_url("https://example.com/") == "https://example.com/"


def test_query_and_path_survive_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_SKIP_DNS_CHECK", "true")
    assert validate_public_http_url("https://example.com/a/b?c=d") == (
        "https://example.com/a/b?c=d"
    )
