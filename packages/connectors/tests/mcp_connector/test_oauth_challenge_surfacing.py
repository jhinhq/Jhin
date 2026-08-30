"""A refused credential is not a transport failure, and is not reported as one.

``McpClient`` folds every handshake failure into one display-safe sentence.
A ``401``/``403`` has to escape that fold, because the three cures are
different — renew the token, widen the grant, replace a pasted secret — and a
caller that only sees "could not connect" cannot choose between them. These
tests pin the escape, at the seam, against a real server.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession

from jhin_connectors.mcp.client import McpAuthChallengeError, McpClient, McpConnectionError
from jhin_connectors.testing.fake_mcp import DEFAULT_TOKEN, FakeMcpServer

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"


@pytest.fixture(scope="module")
def open_mcp() -> Iterator[FakeMcpServer]:
    previous = os.environ.get(ALLOWLIST_ENV)
    with FakeMcpServer(token=None) as server:
        os.environ[ALLOWLIST_ENV] = server.base_url
        try:
            yield server
        finally:
            if previous is None:
                os.environ.pop(ALLOWLIST_ENV, None)
            else:
                os.environ[ALLOWLIST_ENV] = previous


@pytest.fixture(scope="module")
def guarded_mcp() -> Iterator[FakeMcpServer]:
    previous = os.environ.get(ALLOWLIST_ENV)
    with FakeMcpServer(token=DEFAULT_TOKEN) as server:
        os.environ[ALLOWLIST_ENV] = server.base_url
        try:
            yield server
        finally:
            if previous is None:
                os.environ.pop(ALLOWLIST_ENV, None)
            else:
                os.environ[ALLOWLIST_ENV] = previous


async def _tool_count(session: ClientSession) -> int:
    listed = await session.list_tools()
    return len(listed.tools)


async def test_a_401_at_the_handshake_escapes_the_transport_fold(
    guarded_mcp: FakeMcpServer,
) -> None:
    client = McpClient(guarded_mcp.mcp_url, headers={"Authorization": "Bearer wrong-token-value"})
    with pytest.raises(McpAuthChallengeError) as caught:
        await client.run(_tool_count)

    error = caught.value
    assert error.status_code == 401
    # Still a McpConnectionError, so every existing caller keeps working.
    assert isinstance(error, McpConnectionError)
    assert "401" in str(error)
    assert "wrong-token-value" not in str(error)
    assert guarded_mcp.mcp_url not in str(error)


async def test_a_401_raised_inside_the_session_body_escapes_too(
    open_mcp: FakeMcpServer,
) -> None:
    """The transport runs the caller's body inside its own task group and
    re-raises the body's failure outside it. A refusal that surfaces there —
    a token that stopped working between the handshake and the call — has to
    be recognised on that path as well as on the handshake path."""
    challenge = 'Bearer error="invalid_token", scope="mcp:tools"'

    async def body(_session: ClientSession) -> int:
        response = httpx.Response(
            401, headers={"WWW-Authenticate": challenge}, request=httpx.Request("POST", "https://x")
        )
        raise httpx.HTTPStatusError("refused", request=response.request, response=response)

    with pytest.raises(McpAuthChallengeError) as caught:
        await McpClient(open_mcp.mcp_url).run(body)
    assert caught.value.status_code == 401
    assert caught.value.www_authenticate == challenge


async def test_a_body_failure_that_is_not_a_refusal_still_passes_through(
    open_mcp: FakeMcpServer,
) -> None:
    """The escape hatch must not swallow the caller's own exceptions — the
    executor's risk-drift refusal travels this exact path."""

    class Drift(RuntimeError):
        pass

    async def body(_session: ClientSession) -> int:
        raise Drift("the server changed this tool's risk")

    with pytest.raises(Drift):
        await McpClient(open_mcp.mcp_url).run(body)


async def test_an_ordinary_transport_failure_is_still_an_ordinary_failure(
    open_mcp: FakeMcpServer,
) -> None:
    unreachable = f"{open_mcp.base_url}/not-an-mcp-endpoint"
    with pytest.raises(McpConnectionError) as caught:
        await McpClient(unreachable).run(_tool_count)
    assert not isinstance(caught.value, McpAuthChallengeError)
    assert "could not connect to the MCP server" in str(caught.value)


async def test_the_unauthenticated_server_still_works_untouched(
    open_mcp: FakeMcpServer,
) -> None:
    assert await McpClient(open_mcp.mcp_url).run(_tool_count) == 6


def test_a_nested_refusal_is_found_through_an_exception_group() -> None:
    """The SDK raises ``raise_for_status`` errors from inside a task group, so
    the status is normally several layers down."""
    from jhin_connectors.mcp.client import _auth_challenge

    response = httpx.Response(
        403,
        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
        request=httpx.Request("POST", "https://x"),
    )
    inner = httpx.HTTPStatusError("refused", request=response.request, response=response)
    group: BaseException = ExceptionGroup("transport", [RuntimeError("noise"), inner])
    outer: Any = ExceptionGroup("outer", [group])
    found = _auth_challenge(outer)
    assert found is not None
    assert found[0] == 403
    assert "insufficient_scope" in found[1]

    assert _auth_challenge(RuntimeError("nothing to do with auth")) is None
