"""Outbound MCP transport: URL policy, auth headers, and bounded sessions.

Security posture mirrors the other connectors' HTTP clients:

- the server URL is validated before persistence *and* before every
  connection: public ``https`` origins are allowed; anything else (plain
  http, localhost, private ranges, IP literals that are not global) must be
  in the operator allow-list ``JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS``;
- redirects are never followed (the SDK default would follow them);
- every request is bounded by a timeout; tool calls and discovery run inside
  one short-lived session that is closed on every path;
- no error crossing this module carries the URL, headers, or credential.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, cast

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import InitializeResult

from jhin_connectors.endpoints import validate_public_http_url
from jhin_connectors.mcp.manifest import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    TRANSPORT_AUTO,
    TRANSPORT_SSE,
    TRANSPORT_STREAMABLE_HTTP,
    TRANSPORTS,
)

ALLOWLIST_ENV = "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS"
DEFAULT_TIMEOUT_SECONDS = 30.0
SSE_READ_TIMEOUT_SECONDS = 120.0

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
# Headers the transport owns; a connection must not override them.
_RESERVED_HEADERS = frozenset(
    {"host", "content-length", "content-type", "accept", "mcp-session-id", "transfer-encoding"}
)


class McpConnectionError(Exception):
    """A display-safe transport/handshake failure (never URL, headers, token)."""


def validate_mcp_server_url(raw: str, *, allowlist_env: str = ALLOWLIST_ENV) -> str:
    """Return a normalized server URL when policy allows its origin.

    Public ``https`` origins are accepted as-is. Any other origin (``http``,
    localhost, private/link-local hosts, IP literals that are not global)
    needs an exact operator allow-list entry. Userinfo and fragments are
    rejected; the path and query are preserved (MCP endpoints have paths).
    The policy itself lives in :mod:`jhin_connectors.endpoints`, shared with
    the generic HTTP connector.
    """
    return validate_public_http_url(raw, kind="MCP server URL", allowlist_env=allowlist_env)


def validate_header_name(name: object) -> str:
    if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
        raise ValueError("config field 'header_name' must be a valid HTTP header name")
    if name.lower() in _RESERVED_HEADERS or name.lower() == "authorization":
        raise ValueError("config field 'header_name' is reserved")
    return name


def validate_transport(value: object) -> str:
    if not isinstance(value, str) or value not in TRANSPORTS:
        raise ValueError("config field 'transport' must be auto, streamable_http, or sse")
    return value


def auth_headers(
    auth_type: str, credentials: Mapping[str, str], config: Mapping[str, Any]
) -> dict[str, str]:
    """Request headers for one connection's auth scheme. Raises ValueError
    with a credential-free message when the stored material is unusable."""
    if auth_type == AUTH_NONE:
        return {}
    token = credentials.get("token", "")
    if not token:
        raise ValueError("this MCP connection stores no token")
    if any(character in token for character in "\r\n\0"):
        raise ValueError("this MCP connection's token is malformed")
    if auth_type == AUTH_BEARER:
        return {"Authorization": f"Bearer {token}"}
    if auth_type == AUTH_HEADER:
        return {validate_header_name(config.get("header_name")): token}
    raise ValueError(f"unsupported MCP auth type {auth_type!r}")


def _client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Like the SDK default but *never* follows redirects."""
    return httpx.AsyncClient(
        follow_redirects=False,
        headers=headers,
        timeout=timeout if timeout is not None else httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
        auth=auth,
    )


class _BodyFailure(Exception):
    """Wraps an exception raised by the caller's session body so transport
    fallback never re-runs a body that already executed (an effect may have
    happened)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("session body failed")
        self.cause = cause


class McpClient:
    """One connection's transport settings; ``run`` opens a session per call."""

    def __init__(
        self,
        server_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        transport: str = TRANSPORT_AUTO,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = server_url
        self._headers = dict(headers or {})
        self._transport = validate_transport(transport)
        self._timeout = timeout_seconds
        self.initialize_result: InitializeResult | None = None

    async def run[T](self, body: Callable[[ClientSession], Awaitable[T]]) -> T:
        """Open a session (initialize), run ``body`` once, close the session.

        With ``transport=auto`` the Streamable HTTP handshake is tried first;
        if it fails *before the body runs*, the legacy SSE transport is tried.
        """
        order = (
            (TRANSPORT_STREAMABLE_HTTP, TRANSPORT_SSE)
            if self._transport == TRANSPORT_AUTO
            else (self._transport,)
        )
        failures: list[str] = []
        for transport in order:
            try:
                return await self._run_with(transport, body)
            except _BodyFailure as wrapped:
                raise wrapped.cause from None
            except McpConnectionError as error:
                failures.append(f"{transport}: {error}")
            except Exception as error:  # transport/handshake failure, body not run
                failures.append(f"{transport}: {_describe(error)}")
        raise McpConnectionError(
            "could not connect to the MCP server (" + "; ".join(failures) + ")"
        )

    async def _run_with[T](
        self, transport: str, body: Callable[[ClientSession], Awaitable[T]]
    ) -> T:
        outcome: T | None = None
        failure: Exception | None = None
        ran = False
        async with AsyncExitStack() as stack:
            if transport == TRANSPORT_STREAMABLE_HTTP:
                http_client = await stack.enter_async_context(
                    _client_factory(
                        headers=self._headers,
                        timeout=httpx.Timeout(self._timeout, read=SSE_READ_TIMEOUT_SECONDS),
                    )
                )
                read, write, _session_id = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client)
                )
            else:
                read, write = await stack.enter_async_context(
                    sse_client(
                        self._url,
                        headers=self._headers,
                        timeout=self._timeout,
                        sse_read_timeout=SSE_READ_TIMEOUT_SECONDS,
                        httpx_client_factory=_client_factory,
                    )
                )
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self._timeout))
            )
            self.initialize_result = await session.initialize()
            # Never let a body exception propagate through the transport's
            # task group: the session is closed cleanly first, then the
            # failure is re-raised outside it.
            try:
                outcome = await body(session)
                ran = True
            except Exception as error:
                failure = error
        if failure is not None:
            raise _BodyFailure(failure)
        if not ran:  # pragma: no cover - defensive
            raise McpConnectionError("session closed unexpectedly")
        return cast(T, outcome)


def _describe(error: BaseException) -> str:
    """Type-only description; exception messages can echo URLs or bodies."""
    if isinstance(error, BaseExceptionGroup):
        inner = sorted({_describe(member) for member in error.exceptions})
        return ", ".join(inner) or type(error).__name__
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


__all__ = [
    "ALLOWLIST_ENV",
    "McpClient",
    "McpConnectionError",
    "auth_headers",
    "validate_header_name",
    "validate_mcp_server_url",
    "validate_transport",
]
