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

Alternatively the server can be an **OAuth-protected resource**
(``require_oauth=True``): it then answers unauthenticated requests with a
``401`` and an RFC 9728 ``WWW-Authenticate`` challenge, publishes protected
resource metadata at the root and/or path-inserted well-known URLs, and
accepts only bearer tokens a test has told it about. That is enough for the
whole authorization flow to run offline against
:class:`jhin_connectors.testing.fake_oauth.FakeAuthorizationServer`, and for
the interesting failures — a token revoked mid-session, a grant too narrow —
to be produced on demand rather than mocked.

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
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DEFAULT_TOKEN = "fake-mcp-token"
DEFAULT_ACCESS_TOKEN = "fake-mcp-access-token"
DEFAULT_OAUTH_SCOPES: tuple[str, ...] = ("mcp:tools",)
STATE_PATH = "/_state"
RESET_PATH = "/_reset"
PRM_PATH = "/.well-known/oauth-protected-resource"

#: Well-known URLs the OAuth-protected mode is willing to answer on. RFC 9728
#: has clients try the path-inserted URL first and the root second, and real
#: servers are split between them, so the fake can be either or both.
PrmStyle = Literal["both", "root", "path"]

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


@dataclass(frozen=True)
class FakeOAuthConfig:
    """How the OAuth-protected mode presents itself.

    ``authorization_server`` is the issuer a test's fake AS reports; it is the
    only value discovery follows out of this server, so it is the one a test
    normally sets. Everything else has a working default.
    """

    authorization_server: str = ""
    scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES
    prm_style: PrmStyle = "both"
    #: Whether the ``401`` names its metadata document. A server that omits it
    #: forces the client onto the constructed well-known candidates instead.
    advertise_resource_metadata: bool = True
    #: Extra scopes the resource claims to support beyond the ones it requires
    #: — what a step-up challenge can ask for.
    additional_scopes: tuple[str, ...] = ()


class FakeMcpOAuthState:
    """Which bearer tokens this resource currently accepts, and why it might
    refuse. Mutated directly by tests: the server runs in a thread of the same
    process, so there is no need for a control endpoint."""

    def __init__(self, config: FakeOAuthConfig, *, initial_token: str) -> None:
        self._lock = threading.Lock()
        self._accepted: set[str] = {initial_token} if initial_token else set()
        self._revoked: set[str] = set()
        self._accept_unknown = False
        self._required: set[str] = set()
        self.config = config
        self.challenges = 0
        self.authorized_calls = 0
        self.presented_tokens: list[str] = []

    def accept(self, token: str) -> None:
        """Also accept ``token`` from now on."""
        with self._lock:
            self._accepted.add(token)
            self._revoked.discard(token)

    def accept_only(self, token: str) -> None:
        """Accept ``token`` and nothing else — how a test revokes the token a
        connection is holding without breaking the next one it will be given."""
        with self._lock:
            self._accepted = {token}
            self._revoked.clear()
            self._accept_unknown = False

    def revoke(self, token: str) -> None:
        """Refuse ``token`` from here on, and accept any other bearer token.

        That second half is what makes a refresh-then-retry test possible
        without the two fakes sharing a keyring: this resource trusts whatever
        its authorization server issues next, exactly as a real one does after
        validating a token it did not mint.
        """
        with self._lock:
            self._accepted.discard(token)
            self._revoked.add(token)
            self._accept_unknown = True

    def reject_all(self) -> None:
        with self._lock:
            self._accepted.clear()
            self._accept_unknown = False

    def require_scope(self, *scopes: str) -> None:
        """Answer ``403 insufficient_scope`` naming ``scopes`` until cleared."""
        with self._lock:
            self._required = set(scopes)

    def clear_required_scope(self) -> None:
        with self._lock:
            self._required.clear()

    def _verdict(self, token: str | None) -> tuple[int, tuple[str, ...]] | None:
        """``None`` when the request may proceed, else the status to answer
        with and the scopes to name."""
        with self._lock:
            accepted = (
                token is not None
                and token not in self._revoked
                and (token in self._accepted or self._accept_unknown)
            )
            required = tuple(sorted(self._required))
            if not accepted:
                self.challenges += 1
                return 401, self.config.scopes
            if required:
                self.challenges += 1
                return 403, required
            self.authorized_calls += 1
            if token is not None:
                self.presented_tokens.append(token)
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "challenges": self.challenges,
                "authorized_calls": self.authorized_calls,
                "accepted_token_count": len(self._accepted),
                "required_scopes": sorted(self._required),
            }


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


def _quote(value: str) -> str:
    """A quoted-string auth-param value with the two characters that would
    end it removed, so a scope name can never forge another parameter."""
    return '"' + value.replace("\\", "").replace('"', "") + '"'


def _challenge_header(
    oauth: FakeMcpOAuthState, *, status_code: int, scopes: Sequence[str], resource_metadata: str
) -> str:
    params = [f"realm={_quote('mcp')}"]
    if status_code == 401:
        params.append(f"error={_quote('invalid_token')}")
        params.append(f"error_description={_quote('The access token is expired or revoked')}")
    else:
        params.append(f"error={_quote('insufficient_scope')}")
        params.append(f"error_description={_quote('The access token lacks a required scope')}")
    if scopes:
        params.append(f"scope={_quote(' '.join(scopes))}")
    if resource_metadata and oauth.config.advertise_resource_metadata:
        params.append(f"resource_metadata={_quote(resource_metadata)}")
    return "Bearer " + ", ".join(params)


def _prm_document(oauth: FakeMcpOAuthState, *, resource: str) -> dict[str, Any]:
    scopes = [*oauth.config.scopes, *oauth.config.additional_scopes]
    document: dict[str, Any] = {
        "resource": resource,
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
    }
    if oauth.config.authorization_server:
        document["authorization_servers"] = [oauth.config.authorization_server]
    return document


def _prm_routes(oauth: FakeMcpOAuthState, *, resource: str) -> list[Route]:
    """The root and path-inserted well-known URLs, each served only when the
    configured style says this server publishes there."""
    style = oauth.config.prm_style
    resource_path = resource.split("://", 1)[-1].partition("/")[2]

    async def root(_request: Request) -> Response:
        if style not in {"both", "root"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_prm_document(oauth, resource=resource))

    async def inserted(request: Request) -> Response:
        if style not in {"both", "path"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if request.path_params.get("resource_path", "") != resource_path:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_prm_document(oauth, resource=resource))

    return [
        Route(PRM_PATH, root, methods=["GET"]),
        Route(PRM_PATH + "/{resource_path:path}", inserted, methods=["GET"]),
    ]


def _oauth_guard(app: ASGIApp, oauth: FakeMcpOAuthState, *, resource: str) -> ASGIApp:
    """Refuse every MCP request that does not carry an accepted bearer token,
    with the challenge RFC 9728 clients discover from."""
    metadata_url = _metadata_url(resource, style=oauth.config.prm_style)
    open_paths = {STATE_PATH, RESET_PATH}

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or path in open_paths or path.startswith(PRM_PATH):
            await app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw = headers.get("authorization", "")
        token = raw[7:].strip() if raw[:7].lower() == "bearer " else None
        verdict = oauth._verdict(token)
        if verdict is None:
            await app(scope, receive, send)
            return
        status_code, scopes = verdict
        response = JSONResponse(
            {"error": "invalid_token" if status_code == 401 else "insufficient_scope"},
            status_code=status_code,
            headers={
                "WWW-Authenticate": _challenge_header(
                    oauth,
                    status_code=status_code,
                    scopes=scopes,
                    resource_metadata=metadata_url,
                )
            },
        )
        await response(scope, receive, send)

    return guarded


def _metadata_url(resource: str, *, style: PrmStyle) -> str:
    origin, _, path = resource.partition("://")[2].partition("/")
    scheme = resource.split("://", 1)[0]
    if style == "root" or not path:
        return f"{scheme}://{origin}{PRM_PATH}"
    return f"{scheme}://{origin}{PRM_PATH}/{path}"


class _OAuthGuardMiddleware:
    def __init__(self, app: ASGIApp, oauth: FakeMcpOAuthState, resource: str) -> None:
        self._app = _oauth_guard(app, oauth, resource=resource)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


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


def build_app(
    state: FakeMcpState | None = None,
    *,
    token: str | None = None,
    oauth: FakeMcpOAuthState | None = None,
    resource: str = "",
) -> Starlette:
    """Streamable HTTP at ``/mcp`` plus SSE at ``/sse``/``/messages/``.

    Passing ``oauth`` turns the server into an OAuth-protected resource:
    protected resource metadata is published and every MCP request needs an
    accepted bearer token. ``resource`` is the canonical URI the metadata
    claims — a test's ``FakeMcpServer`` knows its own port and supplies it.
    """
    active_state = state if state is not None else FakeMcpState()
    server = build_server(active_state)
    app = server.streamable_http_app()
    app.router.routes.extend(_state_routes(active_state))
    if oauth is not None:
        app.router.routes.extend(_prm_routes(oauth, resource=resource))
    app.router.routes.extend(server.sse_app().routes)
    if token:
        app.add_middleware(_GuardMiddleware, token=token)
    if oauth is not None:
        app.add_middleware(_OAuthGuardMiddleware, oauth=oauth, resource=resource)
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
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = DEFAULT_TOKEN,
        require_oauth: bool = False,
        authorization_server: str = "",
        access_token: str = DEFAULT_ACCESS_TOKEN,
        scopes: Sequence[str] = DEFAULT_OAUTH_SCOPES,
        additional_scopes: Sequence[str] = (),
        prm_style: PrmStyle = "both",
        advertise_resource_metadata: bool = True,
    ) -> None:
        import socket

        import uvicorn

        self.state = FakeMcpState()
        self.token = None if require_oauth else token
        self.access_token = access_token
        if port == 0:
            with socket.socket() as probe:
                probe.bind((host, 0))
                port = probe.getsockname()[1]
        self._host = host
        self._port = port
        # The static-token guard and the OAuth guard answer the same requests
        # in incompatible ways, so an OAuth server drops the static one.
        self.oauth: FakeMcpOAuthState | None = None
        if require_oauth:
            self.oauth = FakeMcpOAuthState(
                FakeOAuthConfig(
                    authorization_server=authorization_server,
                    scopes=tuple(scopes),
                    additional_scopes=tuple(additional_scopes),
                    prm_style=prm_style,
                    advertise_resource_metadata=advertise_resource_metadata,
                ),
                initial_token=access_token,
            )
        config = uvicorn.Config(
            build_app(
                self.state,
                token=self.token,
                oauth=self.oauth,
                resource=f"http://{host}:{port}/mcp",
            ),
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

    @property
    def resource(self) -> str:
        """The RFC 8707 audience this server's tokens are for — the same
        string an OAuth MCP connection must record in ``oauth_resource``."""
        return self.mcp_url

    @property
    def prm_url(self) -> str:
        """The well-known URL the ``401`` challenge points at."""
        style: PrmStyle = self.oauth.config.prm_style if self.oauth is not None else "both"
        return _metadata_url(self.resource, style=style)

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
    issuer = os.environ.get("FAKE_MCP_AUTHORIZATION_SERVER", "").strip()
    oauth: FakeMcpOAuthState | None = None
    resource = os.environ.get("FAKE_MCP_RESOURCE", f"http://localhost:{port}/mcp")
    if issuer:
        token = None
        oauth = FakeMcpOAuthState(
            FakeOAuthConfig(authorization_server=issuer),
            initial_token=os.environ.get("FAKE_MCP_ACCESS_TOKEN", DEFAULT_ACCESS_TOKEN),
        )
    uvicorn.run(
        build_app(token=token, oauth=oauth, resource=resource),
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_ACCESS_TOKEN",
    "DEFAULT_OAUTH_SCOPES",
    "DEFAULT_TOKEN",
    "PRM_PATH",
    "FakeMcpOAuthState",
    "FakeMcpServer",
    "FakeMcpState",
    "FakeOAuthConfig",
    "PrmStyle",
    "build_app",
    "build_server",
    "main",
]
