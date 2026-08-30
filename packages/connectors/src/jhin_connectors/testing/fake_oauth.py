"""A real OAuth 2.1 authorization server, in process, for tests and dev.

Same shape as :class:`~jhin_connectors.testing.fake_mcp.FakeMcpServer`: uvicorn
on a free localhost port in a daemon thread, a context manager, ``/_state`` and
``/_reset``. Nothing here is mocked — it speaks RFC 8414 metadata, RFC 9728
protected-resource metadata, RFC 7591 registration, RFC 7636 PKCE (verified for
real, with a genuine SHA-256 comparison), RFC 8707 resource binding, RFC 8628
device flow, and RFC 7009 revocation over HTTP.

:class:`FakeAsConfig` also carries the hostile switches the security suite
needs — an issuer that lies, metadata that redirects or runs to megabytes, a
server that forgets its own clients, a token endpoint that refuses the audience
— so those paths are exercised against a server that really behaves that way
rather than against a patched function.

Every value it issues is obviously fake. Runs as a pytest fixture, or as
``python -m jhin_connectors.testing.fake_oauth`` on a dev host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

STATE_PATH = "/_state"
RESET_PATH = "/_reset"

WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
WELL_KNOWN_OIDC = "/.well-known/openid-configuration"
WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"

DEFAULT_SCOPES: tuple[str, ...] = ("read", "write", "offline_access")


@dataclass
class FakeAsConfig:
    """Every knob the OAuth test suite turns.

    The defaults describe a well-behaved, modern authorization server: an
    origin-root issuer, OAuth-style metadata, S256, dynamic registration, an
    ``iss`` parameter, and rotating refresh tokens.
    """

    issuer_path: str = ""
    metadata_style: Literal["oauth", "openid", "both"] = "oauth"
    code_challenge_methods: tuple[str, ...] = ("S256",)
    supports_dcr: bool = True
    emits_iss: bool = True
    advertises_iss_support: bool = True
    rotates_refresh_tokens: bool = True
    access_token_lifetime_seconds: int = 3600
    refresh_token_lifetime_seconds: int | None = None
    supports_device_flow: bool = False
    supports_revocation: bool = True
    scopes_supported: tuple[str, ...] = DEFAULT_SCOPES

    # Protected-resource side, so discovery can be exercised without an MCP
    # server standing behind it.
    prm_style: Literal["path_inserted", "root", "both", "none"] = "both"
    prm_resource_override: str | None = None
    prm_authorization_servers: tuple[str, ...] | None = None
    challenge_scope: str | None = "read"
    challenge_quoted: bool = True
    challenge_resource_metadata: bool = True
    # False makes /mcp answer an unauthenticated initialize with a 200, which
    # is how a server that needs no authorization at all behaves.
    require_auth: bool = True

    # Registration behaviour.
    registration_status: int = 201
    registration_issues_secret: bool = False
    registration_auth_method: str = "none"
    registration_includes_rfc7592: bool = True
    reject_web_redirect_uri: bool = False
    registration_error: str | None = None

    # Hostile switches, each toggled by a test.
    metadata_issuer_override: str | None = None
    token_endpoint_override: str | None = None
    forget_client_after: int | None = None
    fail_token_with: str | None = None
    # The HTTP status ``fail_token_with`` answers with. 400 is RFC 6749's
    # answer for a rejected grant; 429/503 exercise the transient path.
    token_error_status: int = 400
    reject_resource: bool = False
    oversized_metadata: bool = False
    redirect_on_metadata: bool = False
    authorize_error: str | None = None
    device_errors_with_http_200: bool = False
    slow_down_once: bool = False


@dataclass
class _Client:
    client_id: str
    client_secret: str | None
    token_endpoint_auth_method: str
    redirect_uris: tuple[str, ...]
    scope: str
    document: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    resource: str


@dataclass
class _DeviceGrant:
    client_id: str
    user_code: str
    scope: str
    approved: bool = False
    denied: bool = False
    expired: bool = False
    polls: int = 0


class FakeAsState:
    """Everything the server has been asked to do, for tests to assert on."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: dict[str, _Client] = {}
        self.codes: dict[str, _Code] = {}
        self.refresh_tokens: dict[str, dict[str, str]] = {}
        self.devices: dict[str, _DeviceGrant] = {}
        self.tokens: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.token_calls: dict[str, int] = {}
        self.revoked: list[str] = []

    def reset(self) -> None:
        with self.lock:
            self.clients.clear()
            self.codes.clear()
            self.refresh_tokens.clear()
            self.devices.clear()
            self.tokens.clear()
            self.requests.clear()
            self.token_calls.clear()
            self.revoked.clear()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "clients": sorted(self.clients),
                "issued_tokens": len(self.tokens),
                "requests": len(self.requests),
                "revoked": list(self.revoked),
            }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _error(code: str, *, status: int = 400, description: str = "fake failure") -> JSONResponse:
    return JSONResponse({"error": code, "error_description": description}, status_code=status)


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {key: value for key, value in data.multi_items() if isinstance(value, str)}


class _Handlers:
    """The server's behaviour, bound to one config and one state object."""

    def __init__(self, config: FakeAsConfig, state: FakeAsState, base_url: str) -> None:
        self.config = config
        self.state = state
        self.base_url = base_url.rstrip("/")

    # -- identity ---------------------------------------------------------

    @property
    def issuer(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}"

    @property
    def prefix(self) -> str:
        return self.config.issuer_path

    def _record(self, request: Request, body: dict[str, Any]) -> None:
        with self.state.lock:
            self.state.requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": dict(request.query_params),
                    "authorization": request.headers.get("authorization", ""),
                    "accept": request.headers.get("accept", ""),
                    "content_type": request.headers.get("content-type", ""),
                    "body": body,
                }
            )

    # -- metadata ---------------------------------------------------------

    def _metadata_document(self) -> dict[str, Any]:
        config = self.config
        document: dict[str, Any] = {
            "issuer": config.metadata_issuer_override or self.issuer,
            "authorization_endpoint": f"{self.base_url}{self.prefix}/authorize",
            "token_endpoint": config.token_endpoint_override
            or f"{self.base_url}{self.prefix}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "scopes_supported": list(config.scopes_supported),
        }
        if config.code_challenge_methods:
            document["code_challenge_methods_supported"] = list(config.code_challenge_methods)
        if config.supports_dcr:
            document["registration_endpoint"] = f"{self.base_url}{self.prefix}/register"
        if config.supports_revocation:
            document["revocation_endpoint"] = f"{self.base_url}{self.prefix}/revoke"
        if config.supports_device_flow:
            document["device_authorization_endpoint"] = f"{self.base_url}{self.prefix}/device/code"
            document["grant_types_supported"].append("urn:ietf:params:oauth:grant-type:device_code")
        if config.advertises_iss_support:
            document["authorization_response_iss_parameter_supported"] = True
        if config.oversized_metadata:
            document["padding"] = "x" * 70_000
        return document

    async def metadata(self, request: Request) -> Response:
        self._record(request, {})
        if self.config.redirect_on_metadata:
            return Response(status_code=302, headers={"Location": f"{self.base_url}/moved"})
        return JSONResponse(self._metadata_document())

    async def missing(self, request: Request) -> Response:
        self._record(request, {})
        return JSONResponse({"error": "not_found"}, status_code=404)

    # -- protected resource ----------------------------------------------

    def _prm_document(self) -> dict[str, Any]:
        config = self.config
        servers = (
            list(config.prm_authorization_servers)
            if config.prm_authorization_servers is not None
            else [self.issuer]
        )
        return {
            "resource": config.prm_resource_override or f"{self.base_url}/mcp",
            "authorization_servers": servers,
            "scopes_supported": list(config.scopes_supported),
        }

    async def protected_resource(self, request: Request) -> Response:
        self._record(request, {})
        path_inserted = request.url.path != WELL_KNOWN_PRM
        style = self.config.prm_style
        if style == "none":
            return JSONResponse({"error": "not_found"}, status_code=404)
        if style == "root" and path_inserted:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if style == "path_inserted" and not path_inserted:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(self._prm_document())

    async def mcp(self, request: Request) -> Response:
        """A protected MCP endpoint: 401 with a Bearer challenge."""
        self._record(request, {})
        if not self.config.require_auth:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "Fake open MCP", "version": "1"},
                    },
                }
            )
        parts: list[str] = ['realm="fake"', 'error="invalid_token"']
        if self.config.challenge_resource_metadata:
            value = f"{self.base_url}{WELL_KNOWN_PRM}/mcp"
            parts.append(
                f'resource_metadata="{value}"'
                if self.config.challenge_quoted
                else f"resource_metadata={value}"
            )
        if self.config.challenge_scope:
            parts.append(f'scope="{self.config.challenge_scope}"')
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)},
        )

    # -- registration -----------------------------------------------------

    async def register(self, request: Request) -> Response:
        body = await request.json()
        self._record(request, dict(body) if isinstance(body, dict) else {})
        if not self.config.supports_dcr:
            return _error("invalid_request", status=404)
        if self.config.registration_error:
            return _error(self.config.registration_error)
        if not isinstance(body, dict):
            return _error("invalid_client_metadata")
        if self.config.reject_web_redirect_uri and body.get("application_type") != "native":
            return _error("invalid_redirect_uri")

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _error("invalid_redirect_uri")

        client_id = f"fake-client-{secrets.token_hex(6)}"
        client_secret = (
            f"fake-client-secret-{secrets.token_hex(8)}"
            if self.config.registration_issues_secret
            else None
        )
        client = _Client(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method=self.config.registration_auth_method,
            redirect_uris=tuple(str(uri) for uri in redirect_uris),
            scope=str(body.get("scope", "")),
            document=dict(body),
        )
        with self.state.lock:
            self.state.clients[client_id] = client

        document: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": list(client.redirect_uris),
            "grant_types": body.get("grant_types", ["authorization_code"]),
            "response_types": body.get("response_types", ["code"]),
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
        }
        if client_secret:
            document["client_secret"] = client_secret
            document["client_secret_expires_at"] = 0
        if self.config.registration_includes_rfc7592:
            document["registration_access_token"] = f"fake-reg-token-{secrets.token_hex(8)}"
            document["registration_client_uri"] = (
                f"{self.base_url}{self.prefix}/register/{client_id}"
            )
        return JSONResponse(document, status_code=self.config.registration_status)

    async def registration_config(self, request: Request) -> Response:
        self._record(request, {})
        client_id = request.path_params.get("client_id", "")
        with self.state.lock:
            self.state.clients.pop(str(client_id), None)
        return Response(status_code=204)

    # -- authorization ----------------------------------------------------

    async def authorize(self, request: Request) -> Response:
        params = dict(request.query_params)
        self._record(request, {})
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        if not redirect_uri:
            return _error("invalid_request")
        if self.config.authorize_error:
            return self._redirect(redirect_uri, {"error": self.config.authorize_error}, state)

        client_id = params.get("client_id", "")
        with self.state.lock:
            known = client_id in self.state.clients
        if not known:
            return self._redirect(redirect_uri, {"error": "unauthorized_client"}, state)
        if params.get("response_type") != "code":
            return self._redirect(redirect_uri, {"error": "unsupported_response_type"}, state)

        code = f"fake-code-{secrets.token_hex(10)}"
        with self.state.lock:
            self.state.codes[code] = _Code(
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=params.get("code_challenge", ""),
                code_challenge_method=params.get("code_challenge_method", ""),
                scope=params.get("scope", ""),
                resource=params.get("resource", ""),
            )
        return self._redirect(redirect_uri, {"code": code}, state)

    def _redirect(self, redirect_uri: str, params: dict[str, str], state: str) -> Response:
        merged = dict(params)
        if state:
            merged["state"] = state
        if self.config.emits_iss:
            merged["iss"] = self.issuer
        separator = "&" if urlsplit(redirect_uri).query else "?"
        location = f"{redirect_uri}{separator}{urlencode(merged)}"
        return Response(status_code=302, headers={"Location": location})

    # -- tokens -----------------------------------------------------------

    def _authenticate(self, request: Request, form: dict[str, str]) -> _Client | None:
        client_id = form.get("client_id", "")
        header = request.headers.get("authorization", "")
        if not client_id and header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                client_id = decoded.split(":", 1)[0]
            except Exception:
                return None
        with self.state.lock:
            client = self.state.clients.get(client_id)
        if client is None:
            return None
        if client.client_secret is None:
            return client
        presented: str | None = form.get("client_secret")
        if presented is None and header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                presented = decoded.split(":", 1)[1]
            except Exception:
                presented = None
        return client if presented == client.client_secret else None

    def _forgotten(self, client_id: str) -> bool:
        limit = self.config.forget_client_after
        if limit is None:
            return False
        with self.state.lock:
            self.state.token_calls[client_id] = self.state.token_calls.get(client_id, 0) + 1
            return self.state.token_calls[client_id] > limit

    def _issue(self, *, client_id: str, scope: str, resource: str) -> dict[str, Any]:
        access_token = f"fake-access-{secrets.token_hex(12)}"
        refresh_token = f"fake-refresh-{secrets.token_hex(12)}"
        document: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self.config.access_token_lifetime_seconds,
            "scope": scope,
        }
        document["refresh_token"] = refresh_token
        if self.config.refresh_token_lifetime_seconds is not None:
            document["refresh_token_expires_in"] = self.config.refresh_token_lifetime_seconds
        with self.state.lock:
            self.state.refresh_tokens[refresh_token] = {
                "client_id": client_id,
                "scope": scope,
                "resource": resource,
            }
            self.state.tokens.append(
                {
                    "client_id": client_id,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "scope": scope,
                    "resource": resource,
                    "issued_at": datetime.now(UTC).isoformat(),
                }
            )
        return document

    async def token(self, request: Request) -> Response:
        form = await _form(request)
        self._record(request, dict(form))
        if self.config.fail_token_with:
            return _error(self.config.fail_token_with, status=self.config.token_error_status)

        grant_type = form.get("grant_type", "")
        if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
            return self._device_token(form)

        client = self._authenticate(request, form)
        if client is None:
            return _error("invalid_client", status=401)
        if self._forgotten(client.client_id):
            return _error("invalid_client", status=401)
        if self.config.reject_resource and form.get("resource"):
            return _error("invalid_target")

        if grant_type == "authorization_code":
            return self._authorization_code_token(client, form)
        if grant_type == "refresh_token":
            return self._refresh_token(client, form)
        return _error("unsupported_grant_type")

    def _authorization_code_token(self, client: _Client, form: dict[str, str]) -> Response:
        with self.state.lock:
            record = self.state.codes.pop(form.get("code", ""), None)
        if record is None or record.client_id != client.client_id:
            return _error("invalid_grant")
        if record.redirect_uri != form.get("redirect_uri", ""):
            return _error("invalid_grant")
        if record.code_challenge:
            verifier = form.get("code_verifier", "")
            if not verifier or _s256(verifier) != record.code_challenge:
                return _error("invalid_grant")
        return JSONResponse(
            self._issue(client_id=client.client_id, scope=record.scope, resource=record.resource)
        )

    def _refresh_token(self, client: _Client, form: dict[str, str]) -> Response:
        presented = form.get("refresh_token", "")
        with self.state.lock:
            record = self.state.refresh_tokens.get(presented)
        if record is None or record["client_id"] != client.client_id:
            return _error("invalid_grant")
        if self.config.rotates_refresh_tokens:
            with self.state.lock:
                self.state.refresh_tokens.pop(presented, None)
            return JSONResponse(
                self._issue(
                    client_id=client.client_id,
                    scope=form.get("scope", record["scope"]),
                    resource=form.get("resource", record["resource"]),
                )
            )
        document = self._issue(
            client_id=client.client_id,
            scope=form.get("scope", record["scope"]),
            resource=form.get("resource", record["resource"]),
        )
        # A non-rotating server keeps the caller's refresh token alive and does
        # not mention it in the response at all.
        with self.state.lock:
            self.state.refresh_tokens.pop(document["refresh_token"], None)
            self.state.refresh_tokens[presented] = record
        document.pop("refresh_token", None)
        return JSONResponse(document)

    # -- device flow ------------------------------------------------------

    async def device_code(self, request: Request) -> Response:
        form = await _form(request)
        self._record(request, dict(form))
        if not self.config.supports_device_flow:
            return self._device_error("device_flow_disabled")
        client_id = form.get("client_id", "")
        with self.state.lock:
            known = client_id in self.state.clients
        if not known:
            return self._device_error("invalid_client")
        device_code = f"fake-device-{secrets.token_hex(10)}"
        user_code = f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        with self.state.lock:
            self.state.devices[device_code] = _DeviceGrant(
                client_id=client_id, user_code=user_code, scope=form.get("scope", "")
            )
        return JSONResponse(
            {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": f"{self.base_url}/device",
                "verification_uri_complete": f"{self.base_url}/device?user_code={user_code}",
                "expires_in": 900,
                "interval": 5,
            }
        )

    def _device_error(self, code: str) -> Response:
        status = 200 if self.config.device_errors_with_http_200 else 400
        return JSONResponse(
            {"error": code, "error_description": "fake failure"}, status_code=status
        )

    def _device_token(self, form: dict[str, str]) -> Response:
        device_code = form.get("device_code", "")
        with self.state.lock:
            grant = self.state.devices.get(device_code)
            if grant is not None:
                grant.polls += 1
        if grant is None:
            return self._device_error("expired_token")
        if grant.denied:
            return self._device_error("access_denied")
        if grant.expired:
            return self._device_error("expired_token")
        if self.config.slow_down_once and grant.polls == 1:
            return self._device_error("slow_down")
        if not grant.approved:
            return self._device_error("authorization_pending")
        return JSONResponse(self._issue(client_id=grant.client_id, scope=grant.scope, resource=""))

    # -- revocation -------------------------------------------------------

    async def revoke(self, request: Request) -> Response:
        form = await _form(request)
        self._record(request, dict(form))
        if not self.config.supports_revocation:
            return _error("invalid_request", status=404)
        token = form.get("token", "")
        with self.state.lock:
            self.state.revoked.append(token)
            self.state.refresh_tokens.pop(token, None)
        return JSONResponse({})

    # -- control ----------------------------------------------------------

    async def state_route(self, _request: Request) -> Response:
        return JSONResponse(self.state.snapshot())

    async def reset_route(self, _request: Request) -> Response:
        self.state.reset()
        return JSONResponse({"ok": True})

    async def moved(self, _request: Request) -> Response:
        return PlainTextResponse("moved")


def build_app(config: FakeAsConfig, state: FakeAsState, base_url: str) -> Starlette:
    """The routing table for one configured fake authorization server."""
    handlers = _Handlers(config, state, base_url)
    prefix = config.issuer_path
    style = config.metadata_style

    routes: list[Route] = [
        Route(STATE_PATH, handlers.state_route, methods=["GET"]),
        Route(RESET_PATH, handlers.reset_route, methods=["POST"]),
        Route("/moved", handlers.moved, methods=["GET"]),
        Route(f"{prefix}/authorize", handlers.authorize, methods=["GET"]),
        Route(f"{prefix}/token", handlers.token, methods=["POST"]),
        Route(f"{prefix}/register", handlers.register, methods=["POST"]),
        Route(
            f"{prefix}/register/{{client_id}}",
            handlers.registration_config,
            methods=["DELETE"],
        ),
        Route(f"{prefix}/revoke", handlers.revoke, methods=["POST"]),
        Route(f"{prefix}/device/code", handlers.device_code, methods=["POST"]),
        Route("/mcp", handlers.mcp, methods=["POST", "GET"]),
        Route(WELL_KNOWN_PRM, handlers.protected_resource, methods=["GET"]),
        Route(f"{WELL_KNOWN_PRM}/mcp", handlers.protected_resource, methods=["GET"]),
    ]

    # The metadata candidates a client will walk, served or withheld so a test
    # can prove the ladder advances in the documented order.
    oauth_path = f"{WELL_KNOWN_AS}{prefix}" if prefix else WELL_KNOWN_AS
    oidc_inserted = f"{WELL_KNOWN_OIDC}{prefix}" if prefix else WELL_KNOWN_OIDC
    oidc_appended = f"{prefix}{WELL_KNOWN_OIDC}" if prefix else WELL_KNOWN_OIDC
    served = {
        "oauth": {oauth_path},
        "openid": {oidc_appended} if prefix else {oidc_inserted},
        "both": {oauth_path, oidc_inserted, oidc_appended},
    }[style]
    for candidate in dict.fromkeys((oauth_path, oidc_inserted, oidc_appended)):
        handler = handlers.metadata if candidate in served else handlers.missing
        routes.append(Route(candidate, handler, methods=["GET"]))

    return Starlette(routes=routes)


class FakeAuthorizationServer:
    """In-process authorization server: uvicorn on a free localhost port."""

    def __init__(
        self,
        config: FakeAsConfig | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        import socket

        import uvicorn

        self.config = config if config is not None else FakeAsConfig()
        self.state = FakeAsState()
        if port == 0:
            with socket.socket() as probe:
                probe.bind((host, 0))
                port = int(probe.getsockname()[1])
        self._host = host
        self._port = port
        base_url = f"http://{host}:{port}"
        server_config = uvicorn.Config(
            build_app(self.config, self.state, base_url),
            host=host,
            port=port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(server_config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def resource(self) -> str:
        return self.config.prm_resource_override or f"{self.base_url}/mcp"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}/authorize"

    @property
    def token_endpoint(self) -> str:
        return self.config.token_endpoint_override or (
            f"{self.base_url}{self.config.issuer_path}/token"
        )

    @property
    def registration_endpoint(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}/register"

    @property
    def revocation_endpoint(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}/revoke"

    @property
    def device_authorization_endpoint(self) -> str:
        return f"{self.base_url}{self.config.issuer_path}/device/code"

    def start(self) -> FakeAuthorizationServer:
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + STATE_PATH, timeout=1):
                    return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("fake authorization server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def __enter__(self) -> FakeAuthorizationServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- driving the flow without a browser --------------------------------

    def authorize(self, url: str) -> str:
        """The ``Location`` the authorization endpoint would send a browser to.

        Tests drive the redirect step through this instead of pretending to be
        a user agent, so the code they exchange is one this server really
        issued for the challenge it really received.
        """
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(url, timeout=5) as response:
                location = response.headers.get("Location")
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            error.close()
        if not location:
            raise AssertionError("the fake authorization endpoint issued no redirect")
        return str(location)

    def register_static_client(
        self,
        *,
        client_id: str = "fake-static-client",
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "none",
        redirect_uri: str = "https://jhin.example/api/v1/oauth/callback",
    ) -> str:
        """Pre-register a client, the way an operator pasting one would."""
        with self.state.lock:
            self.state.clients[client_id] = _Client(
                client_id=client_id,
                client_secret=client_secret,
                token_endpoint_auth_method=token_endpoint_auth_method,
                redirect_uris=(redirect_uri,),
                scope="",
            )
        return client_id

    def approve_device(self, user_code: str) -> None:
        with self.state.lock:
            for grant in self.state.devices.values():
                if grant.user_code == user_code:
                    grant.approved = True

    def deny_device(self, user_code: str) -> None:
        with self.state.lock:
            for grant in self.state.devices.values():
                if grant.user_code == user_code:
                    grant.denied = True

    def expire_device(self, user_code: str) -> None:
        with self.state.lock:
            for grant in self.state.devices.values():
                if grant.user_code == user_code:
                    grant.expired = True

    def registered_clients(self) -> dict[str, dict[str, Any]]:
        with self.state.lock:
            return {
                client_id: dict(client.document) for client_id, client in self.state.clients.items()
            }

    def issued_tokens(self) -> list[dict[str, Any]]:
        with self.state.lock:
            return [dict(token) for token in self.state.tokens]

    def recorded_requests(self, *, path_suffix: str | None = None) -> list[dict[str, Any]]:
        with self.state.lock:
            records = [dict(record) for record in self.state.requests]
        if path_suffix is None:
            return records
        return [record for record in records if str(record["path"]).endswith(path_suffix)]

    def revoked_tokens(self) -> list[str]:
        with self.state.lock:
            return list(self.state.revoked)

    def reset(self) -> None:
        self.state.reset()

    def snapshot(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url + STATE_PATH, timeout=3) as response:
            return dict(json.loads(response.read().decode()))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow, so the Location survives."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def main() -> None:
    import uvicorn

    host = os.environ.get("FAKE_OAUTH_HOST", "0.0.0.0")
    port = int(os.environ.get("FAKE_OAUTH_PORT", "8081"))
    base_url = os.environ.get("FAKE_OAUTH_BASE_URL", f"http://127.0.0.1:{port}")
    config = FakeAsConfig(supports_device_flow=True)
    uvicorn.run(build_app(config, FakeAsState(), base_url), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = [
    "DEFAULT_SCOPES",
    "FakeAsConfig",
    "FakeAsState",
    "FakeAuthorizationServer",
    "build_app",
    "main",
]
