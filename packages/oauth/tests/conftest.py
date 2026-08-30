"""Fixtures: real in-process authorization servers, and a real HTTP client.

Nothing here patches ``httpx`` or stubs a response. Every test drives
:class:`~jhin_connectors.testing.fake_oauth.FakeAuthorizationServer` over a
loopback socket, which is the only way the redirect-refusal, size-bound, and
content-type paths get exercised at all — a mocked transport would answer
whatever the mock was told to.

Each started server's origin is added to
``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS`` for the fixture's lifetime and removed
after, exactly as the MCP connector's own end-to-end tests do. DNS resolution
checks are switched off for this package: every assertion here is about the
*lexical* policy, and a live lookup for ``mcp.example.com`` would make these
tests depend on the machine's resolver.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest

from jhin_connectors.testing.fake_oauth import FakeAsConfig, FakeAuthorizationServer

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
SKIP_DNS_ENV = "JHIN_CONNECTOR_SKIP_DNS_CHECK"

StartServer = Callable[..., FakeAuthorizationServer]


@pytest.fixture(autouse=True)
def _skip_dns_checks() -> Iterator[None]:
    previous = os.environ.get(SKIP_DNS_ENV)
    os.environ[SKIP_DNS_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SKIP_DNS_ENV, None)
        else:
            os.environ[SKIP_DNS_ENV] = previous


@pytest.fixture
def start_server() -> Iterator[StartServer]:
    """Start one or more fake authorization servers, allow-listed as they go."""
    servers: list[FakeAuthorizationServer] = []
    previous = os.environ.get(ALLOWLIST_ENV)

    def start(config: FakeAsConfig | None = None) -> FakeAuthorizationServer:
        server = FakeAuthorizationServer(config).start()
        servers.append(server)
        os.environ[ALLOWLIST_ENV] = ",".join(started.base_url for started in servers)
        return server

    try:
        yield start
    finally:
        for server in servers:
            server.stop()
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous


@pytest.fixture
def fake_as(start_server: StartServer) -> FakeAuthorizationServer:
    """A well-behaved modern authorization server."""
    return start_server(FakeAsConfig())


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        yield client


@pytest.fixture
def allow_origins() -> Iterator[Callable[[str], None]]:
    """Add an extra origin to the outbound allow-list for one test."""
    previous = os.environ.get(ALLOWLIST_ENV)

    def allow(origin: str) -> None:
        current = os.environ.get(ALLOWLIST_ENV, "")
        os.environ[ALLOWLIST_ENV] = f"{current},{origin}" if current else origin

    try:
        yield allow
    finally:
        if previous is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = previous
