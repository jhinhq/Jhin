"""In-process fake of GitHub's OAuth surface, quirks included.

GitHub is not an RFC 8414 authorization server and does not behave like one,
so it gets its own fake rather than a :class:`FakeAsConfig` preset:

- device-flow errors come back with **HTTP 200** and the error in the body,
  so a client that only inspects the status code silently treats
  ``authorization_pending`` as success;
- responses are ``application/x-www-form-urlencoded`` unless the request
  carries ``Accept: application/json``, which is why every Jhin call sends it;
- the device flow takes **no client secret** at any step, including refresh —
  the property that makes it the answer for instances a provider cannot reach;
- access tokens live 8 hours (``expires_in`` 28800) and refresh tokens six
  months (``refresh_token_expires_in`` 15897600);
- ``POST /app-manifests/{code}/conversions`` mints a whole GitHub App:
  client id and secret, webhook secret, and a private key.

Every credential minted here is an obvious placeholder (``fake-*``, and a PEM
that is not a key), so a value escaping into a log is unmistakable.
"""

from __future__ import annotations

import secrets as stdlib_secrets
import string
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from jhin_connectors.testing.fake_oauth import RESET_PATH, STATE_PATH


def _free_port(host: str) -> int:
    import socket

    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _user_code() -> str:
    """A GitHub-shaped display code: two groups of four letters, hyphenated."""
    letters = "".join(stdlib_secrets.choice(string.ascii_uppercase) for _ in range(8))
    return f"{letters[:4]}-{letters[4:]}"


DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

ACCESS_TOKEN_LIFETIME_SECONDS = 28_800
REFRESH_TOKEN_LIFETIME_SECONDS = 15_897_600


# Not a key. A PEM-shaped placeholder, so anything that leaks is obvious.
FAKE_PRIVATE_KEY_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "bm90LWEta2V5LXRoaXMtaXMtYS1mYWtlLWZvci1qaGluLXRlc3Rz\n"
    "-----END RSA PRIVATE KEY-----\n"
)


@dataclass(frozen=True)
class FakeGitHubOAuthConfig:
    """Which of GitHub's behaviours this instance exhibits."""

    device_flow_enabled: bool = True
    # One entry consumed per poll: authorization_pending, slow_down,
    # access_denied, expired_token, incorrect_device_code, or ok. An empty
    # script approves on the first poll.
    device_poll_script: tuple[str, ...] = ()
    device_interval_seconds: int = 5
    # GitHub answers device-flow errors with 200 and the error in the body.
    errors_use_http_200: bool = True
    manifest_conversion_status: int = 201
    app_slug: str = "jhin-fake-instance"
    app_id: str = "424242"
    # When set, an ``authorization_code`` exchange whose ``client_secret``
    # differs answers ``incorrect_client_credentials`` — the way GitHub
    # reports a wrong secret, with HTTP 200. Unset keeps the permissive
    # behaviour every existing test relies on.
    expected_client_secret: str | None = None
    # A scripted refusal for the *next* ``authorization_code`` exchange
    # (``redirect_uri_mismatch``, ``bad_verification_code``…), consumed once;
    # ``reset()`` re-arms it. See :meth:`FakeGitHubOAuthServer.refuse_next_exchange`.
    authorization_code_error: str | None = None


class FakeGitHubOAuthServer:
    """GitHub's device flow, token endpoint, and App-manifest conversion."""

    def __init__(
        self,
        config: FakeGitHubOAuthConfig | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.config = config if config is not None else FakeGitHubOAuthConfig()
        self.lock = threading.Lock()
        self.device_code_requests: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.conversion_requests: list[dict[str, Any]] = []
        self.device_grants: dict[str, dict[str, Any]] = {}
        self.issued: list[dict[str, Any]] = []
        self._poll_queue: list[str] = list(self.config.device_poll_script)
        self._next_exchange_error: str | None = self.config.authorization_code_error
        import uvicorn

        self._host = host
        self._port = port or _free_port(host)
        server_config = uvicorn.Config(
            self._build_app(),
            host=self._host,
            port=self._port,
            log_level="warning",
            lifespan="off",
        )
        self._server = uvicorn.Server(server_config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def device_code_url(self) -> str:
        return f"{self.base_url}/login/device/code"

    @property
    def device_token_url(self) -> str:
        return f"{self.base_url}/login/oauth/access_token"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/login/oauth/authorize"

    def start(self) -> FakeGitHubOAuthServer:
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + STATE_PATH, timeout=1):
                    return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("fake GitHub OAuth server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def __enter__(self) -> FakeGitHubOAuthServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def reset(self) -> None:
        with self.lock:
            self.device_code_requests.clear()
            self.token_requests.clear()
            self.conversion_requests.clear()
            self.device_grants.clear()
            self.issued.clear()
            self._poll_queue = list(self.config.device_poll_script)
            self._next_exchange_error = self.config.authorization_code_error

    def refuse_next_exchange(self, error_code: str) -> None:
        """Script one refusal for the next ``authorization_code`` exchange."""
        with self.lock:
            self._next_exchange_error = error_code

    def approve_device(self, user_code: str) -> None:
        with self.lock:
            for grant in self.device_grants.values():
                if grant["user_code"] == user_code:
                    grant["approved"] = True

    def recorded_bodies(self) -> list[dict[str, str]]:
        """Every device/token request body, for 'no secret was sent' asserts."""
        with self.lock:
            return [dict(record["form"]) for record in self.token_requests]

    # -- app ---------------------------------------------------------------

    def _respond(self, request: Request, payload: dict[str, Any], *, status: int = 200) -> Response:
        accept = request.headers.get("accept", "")
        if "application/json" in accept.lower():
            return JSONResponse(payload, status_code=status)
        return PlainTextResponse(
            urllib.parse.urlencode(payload),
            status_code=status,
            media_type="application/x-www-form-urlencoded",
        )

    def _error(self, request: Request, code: str) -> Response:
        status = 200 if self.config.errors_use_http_200 else 400
        return self._respond(
            request,
            {
                "error": code,
                "error_description": f"fake GitHub says {code}",
                "error_uri": "https://docs.github.com/",
            },
            status=status,
        )

    def _build_app(self) -> Starlette:
        async def device_code(request: Request) -> Response:
            form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
            with self.lock:
                self.device_code_requests.append({"form": dict(form)})
            if not self.config.device_flow_enabled:
                return self._error(request, "device_flow_disabled")
            code = f"fake-device-code-{stdlib_secrets.token_hex(8)}"
            display_code = _user_code()
            with self.lock:
                self.device_grants[code] = {
                    "user_code": display_code,
                    "approved": False,
                    "scope": form.get("scope", ""),
                    "client_id": form.get("client_id", ""),
                }
            return self._respond(
                request,
                {
                    "device_code": code,
                    "user_code": display_code,
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": self.config.device_interval_seconds,
                },
            )

        async def access_token(request: Request) -> Response:
            form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
            with self.lock:
                self.token_requests.append(
                    {
                        "form": dict(form),
                        "accept": request.headers.get("accept", ""),
                        "authorization": request.headers.get("authorization", ""),
                    }
                )
            grant_type = form.get("grant_type", "")
            if grant_type == DEVICE_GRANT_TYPE:
                if not self.config.device_flow_enabled:
                    return self._error(request, "device_flow_disabled")
                with self.lock:
                    grant = self.device_grants.get(form.get("device_code", ""))
                    scripted = self._poll_queue.pop(0) if self._poll_queue else None
                if grant is None:
                    return self._error(request, "incorrect_device_code")
                if scripted is not None and scripted != "ok":
                    return self._error(request, scripted)
                if scripted is None and not grant["approved"]:
                    return self._error(request, "authorization_pending")
                return self._issue(request, scope=str(grant["scope"]))
            if grant_type == "refresh_token":
                if not form.get("refresh_token"):
                    return self._error(request, "invalid_grant")
                return self._issue(request, scope=form.get("scope", ""))
            if grant_type in {"", "authorization_code"}:
                if not form.get("code"):
                    return self._error(request, "invalid_grant")
                with self.lock:
                    scripted_error, self._next_exchange_error = self._next_exchange_error, None
                if scripted_error is not None:
                    return self._error(request, scripted_error)
                expected = self.config.expected_client_secret
                if expected is not None and form.get("client_secret") != expected:
                    return self._error(request, "incorrect_client_credentials")
                return self._issue(request, scope=form.get("scope", ""))
            return self._error(request, "unsupported_grant_type")

        async def conversions(request: Request) -> Response:
            code = request.path_params["code"]
            with self.lock:
                self.conversion_requests.append(
                    {"code": code, "accept": request.headers.get("accept", "")}
                )
            return JSONResponse(
                {
                    "id": int(self.config.app_id),
                    "slug": self.config.app_slug,
                    "node_id": "fake-node-id",
                    "name": "Jhin (fake instance)",
                    "client_id": f"Iv1.fake{stdlib_secrets.token_hex(6)}",
                    "client_secret": f"fake-github-client-secret-{stdlib_secrets.token_hex(10)}",
                    "webhook_secret": f"fake-github-webhook-secret-{stdlib_secrets.token_hex(10)}",
                    "pem": FAKE_PRIVATE_KEY_PEM,
                    "html_url": f"{self.base_url}/apps/{self.config.app_slug}",
                },
                status_code=self.config.manifest_conversion_status,
            )

        async def authorize(request: Request) -> Response:
            params = dict(request.query_params)
            redirect_uri = params.get("redirect_uri", "")
            query = {"code": f"fake-github-code-{stdlib_secrets.token_hex(8)}"}
            if params.get("state"):
                query["state"] = params["state"]
            if not redirect_uri:
                return PlainTextResponse("missing redirect_uri", status_code=400)
            return Response(
                status_code=302,
                headers={"Location": f"{redirect_uri}?{urllib.parse.urlencode(query)}"},
            )

        async def state_route(_request: Request) -> Response:
            with self.lock:
                return JSONResponse(
                    {
                        "device_code_requests": len(self.device_code_requests),
                        "token_requests": len(self.token_requests),
                        "conversions": len(self.conversion_requests),
                    }
                )

        async def reset_route(_request: Request) -> Response:
            self.reset()
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route(STATE_PATH, state_route, methods=["GET"]),
                Route(RESET_PATH, reset_route, methods=["POST"]),
                Route("/login/device/code", device_code, methods=["POST"]),
                Route("/login/oauth/access_token", access_token, methods=["POST"]),
                Route("/login/oauth/authorize", authorize, methods=["GET"]),
                Route("/app-manifests/{code}/conversions", conversions, methods=["POST"]),
            ]
        )

    def _issue(self, request: Request, *, scope: str) -> Response:
        access_token = f"ghu_fake{stdlib_secrets.token_hex(12)}"
        refresh_token = f"ghr_fake{stdlib_secrets.token_hex(12)}"
        with self.lock:
            self.issued.append({"access_token": access_token, "refresh_token": refresh_token})
        return self._respond(
            request,
            {
                "access_token": access_token,
                "token_type": "bearer",
                "scope": scope,
                "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
                "refresh_token": refresh_token,
                "refresh_token_expires_in": REFRESH_TOKEN_LIFETIME_SECONDS,
            },
        )


__all__ = [
    "ACCESS_TOKEN_LIFETIME_SECONDS",
    "FAKE_PRIVATE_KEY_PEM",
    "REFRESH_TOKEN_LIFETIME_SECONDS",
    "FakeGitHubOAuthConfig",
    "FakeGitHubOAuthServer",
]
