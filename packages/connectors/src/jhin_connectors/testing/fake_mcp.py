"""A small fake MCP server for dev and tests (plan 32.2 pattern).

Built on the official SDK's ``FastMCP`` so it speaks real Streamable HTTP
(``/mcp``) and legacy SSE (``/sse`` + ``/messages/``). It exposes tools that
exercise every risk mapping and output-sanitization path of the MCP
connector:

- ``echo`` — read-only (``readOnlyHint``), returns the text;
- ``create_note`` — write (explicit non-destructive), keeps an in-memory note;
- ``delete_everything`` — destructive (``destructiveHint``), approval-gated;
- ``picture`` — read-only, returns an image block (stripped to a placeholder);
- ``huge_text`` — read-only, returns hundreds of KB of text (truncated);
- ``unannotated`` — no annotations at all (maps to write by default).

Auth: when ``FAKE_MCP_TOKEN`` is set (default ``fake-mcp-token``), requests
must carry ``Authorization: Bearer <token>`` or ``X-Fake-Mcp-Key: <token>``;
set it to an empty string for an unauthenticated server.

Runs as a pytest fixture (``FakeMcpServer``), on a dev host, or as the
``fake-mcp`` compose service (``python -m jhin_connectors.testing.fake_mcp``).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DEFAULT_TOKEN = "fake-mcp-token"
STATE_PATH = "/_state"
RESET_PATH = "/_reset"

# A 1x1 transparent PNG.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class FakeMcpState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.notes: list[dict[str, str]] = []
        self.deleted = 0
        self.calls: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"notes": list(self.notes), "deleted": self.deleted, "calls": list(self.calls)}

    def reset(self) -> None:
        with self.lock:
            self.notes.clear()
            self.deleted = 0
            self.calls.clear()


def build_server(state: FakeMcpState) -> FastMCP:
    server = FastMCP(
        "Fake MCP",
        instructions="A fake MCP server used by Jhin's tests and dev stack.",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(
        description="Return the given text unchanged.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def echo(text: str) -> str:
        with state.lock:
            state.calls.append("echo")
        return text

    @server.tool(
        description="Create a note with a title and body.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    def create_note(title: str, body: str = "") -> dict[str, Any]:
        note = {"title": title, "body": body}
        with state.lock:
            state.notes.append(note)
            state.calls.append("create_note")
            count = len(state.notes)
        return {"created": True, "title": title, "note_count": count}

    @server.tool(
        description="Delete every note. Irreversible.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def delete_everything(confirm: bool = False) -> str:
        with state.lock:
            state.calls.append("delete_everything")
            if not confirm:
                return "nothing deleted (confirm=false)"
            removed = len(state.notes)
            state.notes.clear()
            state.deleted += removed
        return f"deleted {removed} note(s)"

    @server.tool(
        description="Return a tiny PNG image.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def picture() -> Image:
        with state.lock:
            state.calls.append("picture")
        return Image(data=_PNG_1X1, format="png")

    @server.tool(
        description="Return a very large block of text.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def huge_text(kilobytes: int = 200) -> str:
        with state.lock:
            state.calls.append("huge_text")
        size = max(1, min(int(kilobytes), 2_000)) * 1_024
        return ("lorem ipsum dolor sit amet " * (size // 27 + 1))[:size]

    @server.tool(description="A tool with no annotations at all.")
    def unannotated(value: str = "") -> str:
        with state.lock:
            state.calls.append("unannotated")
        return f"unannotated:{value}"

    return server


def _state_routes(state: FakeMcpState) -> list[Route]:
    async def state_route(_request: Request) -> Response:
        return JSONResponse(state.snapshot())

    async def reset_route(_request: Request) -> Response:
        state.reset()
        return JSONResponse({"ok": True})

    return [
        Route(STATE_PATH, state_route, methods=["GET"]),
        Route(RESET_PATH, reset_route, methods=["POST"]),
    ]


def _token_guard(app: ASGIApp, token: str) -> ASGIApp:
    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path", "") not in {STATE_PATH, RESET_PATH}:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            authorized = headers.get("authorization") == f"Bearer {token}" or (
                headers.get("x-fake-mcp-key") == token
            )
            if not authorized:
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded


def build_app(state: FakeMcpState | None = None, *, token: str | None = None) -> Starlette:
    """Streamable HTTP at ``/mcp`` plus SSE at ``/sse``/``/messages/``."""
    active_state = state if state is not None else FakeMcpState()
    server = build_server(active_state)
    app = server.streamable_http_app()
    app.router.routes.extend(_state_routes(active_state))
    app.router.routes.extend(server.sse_app().routes)
    if token:
        app.add_middleware(_GuardMiddleware, token=token)
    return app


class _GuardMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = _token_guard(app, token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


class FakeMcpServer:
    """In-process server for tests: uvicorn on a free localhost port in a
    daemon thread. ``mcp_url`` is the Streamable HTTP endpoint."""

    def __init__(
        self, *, host: str = "127.0.0.1", port: int = 0, token: str | None = DEFAULT_TOKEN
    ) -> None:
        import socket

        import uvicorn

        self.state = FakeMcpState()
        self.token = token
        if port == 0:
            with socket.socket() as probe:
                probe.bind((host, 0))
                port = probe.getsockname()[1]
        self._host = host
        self._port = port
        config = uvicorn.Config(
            build_app(self.state, token=token),
            host=host,
            port=port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def sse_url(self) -> str:
        return f"{self.base_url}/sse"

    def start(self) -> FakeMcpServer:
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + STATE_PATH, timeout=1):
                    return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("fake MCP server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def __enter__(self) -> FakeMcpServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def snapshot(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url + STATE_PATH, timeout=3) as response:
            return dict(json.loads(response.read().decode()))


def main() -> None:
    import uvicorn

    port = int(os.environ.get("FAKE_MCP_PORT", "8080"))
    token = os.environ.get("FAKE_MCP_TOKEN", DEFAULT_TOKEN) or None
    uvicorn.run(build_app(token=token), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = ["DEFAULT_TOKEN", "FakeMcpServer", "FakeMcpState", "build_app", "build_server", "main"]
