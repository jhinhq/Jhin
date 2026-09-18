import base64
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.http11 import Response as WebSocketResponse

from jhin_tool_worker.preview_gateway import (
    install_preview_routes,
    preview_headers,
    rewrite_preview_body,
    scoped_redirect,
    upstream_path,
)


@pytest.mark.parametrize("extensible", [True, False])
def test_cookie_free_bootstrap_handles_opaque_document_before_app_code(extensible):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the browser bootstrap contract")
    sid = uuid4()
    rendered = rewrite_preview_body(
        b"<html><head><script>appReadsCookies()</script></head></html>",
        "text/html",
        f"/runtime/previews/{sid}/ticket",
        sid,
    ).decode()
    bootstrap = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(
        [
            node,
            "-e",
            r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const ctx=vm.createContext({});
vm.runInContext(`
let nativeReads=0,nativeWrites=0;
const document=Object.create({});
Object.defineProperty(Object.getPrototypeOf(document),'cookie',{
  configurable:true,
  get(){nativeReads++;throw new Error('SecurityError: opaque origin');},
  set(v){nativeWrites++;throw new Error('SecurityError: opaque origin');}
});
const window=globalThis;
window.fetch=()=>{};
window.WebSocket=class {};
const XMLHttpRequest=class {open(){}};
const history={pushState(){},replaceState(){}};
`,ctx);
if(!input.extensible)vm.runInContext('Object.preventExtensions(document)',ctx);
vm.runInContext(input.bootstrap,ctx);
const state=vm.runInContext(`(()=>{
  const before=document.cookie;
  document.cookie='session=never-store; Path=/';
  const after=document.cookie;
  const hydrated=!document.cookie.includes('__next');
  return JSON.stringify({before,after,nativeReads,nativeWrites,hydrated});
})()`,ctx);
assert.deepEqual(JSON.parse(state),{before:'',after:'',nativeReads:0,nativeWrites:0,hydrated:true});
process.stdout.write(state);
""",
        ],
        input=json.dumps({"bootstrap": bootstrap, "extensible": extensible}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hydrated"] is True
    assert rendered.index(bootstrap) < rendered.index("appReadsCookies()")
    assert (
        "allow-same-origin"
        not in preview_headers(f"/runtime/previews/{sid}/ticket", "http://jhin.test")[
            "Content-Security-Policy"
        ]
    )


@pytest.mark.parametrize(
    "failure",
    [
        InvalidStatus(WebSocketResponse(404, "Not Found", Headers())),
        InvalidHandshake("Connection failed before upgrade"),
    ],
)
async def test_preview_websocket_handshake_failure_closes_without_escaping(failure, monkeypatch):
    from jhin_tool_worker import preview_gateway as gateway

    @asynccontextmanager
    async def factory():
        yield object()

    async def ticket_session(*args, **kwargs):
        return SimpleNamespace(config_json={"origin": "http://jhin.test"}), SimpleNamespace(
            kind="preview", config_json={"framework": "static"}
        )

    @asynccontextmanager
    async def failed_connection(*args, **kwargs):
        raise failure
        yield  # pragma: no cover

    monkeypatch.setattr(gateway, "connect", failed_connection)
    monkeypatch.setattr(gateway, "runner_config", lambda: ("http://runner:8085", "test-only-token"))
    app = FastAPI()
    install_preview_routes(app, factory, ticket_session, AsyncMock())
    socket = SimpleNamespace(
        headers={"origin": "null"},
        url=SimpleNamespace(scheme="ws", netloc="jhin.test", query=""),
        scope={"subprotocols": []},
        accept=AsyncMock(),
        close=AsyncMock(),
    )
    endpoint = next(
        route.endpoint for route in app.routes if "websocket" in type(route).__name__.lower()
    )
    await endpoint(socket, uuid4(), "valid", "")
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(code=1008)


def test_scoped_redirect_keeps_stable_root_query_and_relative_assets():
    sid = uuid4()
    stable = f"/runtime/previews/{sid}"
    prefix = stable + "/ticket"
    assert scoped_redirect(stable + "?q=x#part", prefix, sid) == prefix + "/?q=x#part"
    assert scoped_redirect(stable + "/assets/main.js", prefix, sid) == prefix + "/assets/main.js"
    assert scoped_redirect("/docs/?q=../..", prefix, sid) == prefix + "/docs/?q=../.."


def test_preview_paths_rewrite_scoped_assets_and_preserve_binary():
    sid = uuid4()
    stable = f"/runtime/previews/{sid}"
    prefix = stable + "/capability"
    source = (
        f'<html><head><script src="{stable}/client.js"></script></head>'
        '<body><img src="/logo.png"></body></html>'
    ).encode()
    text = rewrite_preview_body(source, "text/html", prefix, sid).decode()
    assert f'src="{prefix}/client.js"' in text
    assert f'src="{prefix}/logo.png"' in text
    assert 'credentials:"omit"' in text
    assert rewrite_preview_body(b"\x00\xff", "image/png", prefix, sid) == b"\x00\xff"
    assert upstream_path("api/echo", "q=x", sid, "next") == stable + "/api/echo?q=x"
    assert (
        "allow-same-origin"
        not in preview_headers(prefix, "http://localhost:3000")["Content-Security-Policy"]
    )


async def test_every_request_checks_ticket_and_strips_credentials_including_errors():
    sid = uuid4()
    requests = []
    revoked = False

    @asynccontextmanager
    async def factory():
        yield object()

    async def ticket_session(db, session_id, token, **kwargs):
        if revoked or token != "valid":
            raise HTTPException(401, "Expired")
        return SimpleNamespace(config_json={"origin": "http://jhin.test"}), SimpleNamespace(
            kind="preview", config_json={"framework": "static"}
        )

    async def runner(method, path, payload):
        requests.append(payload)
        return {
            "status": 200,
            "headers": {"Content-Type": "text/html", "Set-Cookie": "steal=true"},
            "body_base64": base64.b64encode(b"<h1>Hello</h1>").decode(),
        }

    app = FastAPI()
    install_preview_routes(app, factory, ticket_session, runner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://jhin.test"
    ) as client:
        result = await client.post(
            f"/runtime/previews/{sid}/valid/api",
            content=b"data",
            headers={
                "origin": "null",
                "cookie": "private=x",
                "authorization": "Bearer private",
                "content-type": "text/plain",
            },
        )
        assert result.status_code == 200, result.text
        assert "set-cookie" not in result.headers
        assert (
            "cookie" not in requests[0]["headers"] and "authorization" not in requests[0]["headers"]
        )
        assert base64.b64decode(requests[0]["body_base64"]) == b"data"
        assert result.headers["access-control-allow-origin"] == "null"
        blocked = await client.get(
            f"/runtime/previews/{sid}/valid/", headers={"origin": "https://attacker.test"}
        )
        assert blocked.status_code == 403
        assert "sandbox" in blocked.headers["content-security-policy"]
        revoked = True
        expired = await client.get(f"/runtime/previews/{sid}/valid/")
        assert expired.status_code == 401
        assert "sandbox" in expired.headers["content-security-policy"]
        assert len(requests) == 1


async def test_reverse_proxy_uses_ticket_origin_and_authenticated_relative_root_redirect():
    sid = uuid4()
    prefix = f"/runtime/previews/{sid}/valid"
    calls = []

    @asynccontextmanager
    async def factory():
        yield object()

    async def ticket_session(db, session_id, token, **kwargs):
        if token != "valid":
            raise HTTPException(401, "Expired")
        return SimpleNamespace(config_json={"origin": "http://192.168.1.10:3000"}), SimpleNamespace(
            kind="preview", config_json={"framework": "static"}
        )

    async def runner(method, path, payload):
        calls.append(payload)
        return {
            "status": 200,
            "headers": {"Content-Type": "text/html"},
            "body_base64": base64.b64encode(b"<h1>Preview</h1>").decode(),
        }

    app = FastAPI()
    install_preview_routes(app, factory, ticket_session, runner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://runtime-gateway:8086"
    ) as client:
        response = await client.head(prefix + "/", headers={"x-forwarded-host": "attacker.test"})
        assert response.status_code == 200
        policy = response.headers["content-security-policy"]
        assert f"http://192.168.1.10:3000{prefix}/" in policy
        assert "runtime-gateway" not in policy and "attacker.test" not in policy
        assert calls[0]["method"] == "HEAD"
        redirect = await client.get(prefix)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == prefix + "/"
        assert "runtime-gateway" not in redirect.headers["content-security-policy"]
        expired = await client.get(f"/runtime/previews/{sid}/expired")
        assert expired.status_code == 401
        assert "location" not in expired.headers
        assert len(calls) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "https://jhin.test/; connect-src *; x-src https://a.test",
        "https://jhin.test?connect-src=*",
        "https://jhin.test#fragment",
        "https://user:password@jhin.test",
        "https://jhin.test\n",
    ],
)
def test_preview_csp_rejects_non_origin_ticket_values(origin):
    policy = preview_headers("/runtime/previews/session/ticket", origin)["Content-Security-Policy"]
    assert "connect-src *" not in policy
    assert "http://invalid.local/runtime/previews/session/ticket/" in policy


@pytest.mark.parametrize(
    "location",
    [
        "/../../../../api/v1/workspaces",
        "/%2e%2e/%2E%2E/api",
        "/%252e%252e/api",
        "/folder/../api",
        "/folder/./api",
        "/%5c%5cattacker.test/path",
        "/\\attacker.test/path",
        "/foo\nLocation: https://attacker.test",
        "//attacker.test/",
    ],
)
async def test_upstream_redirect_cannot_escape_preview_capability(location):
    sid = uuid4()

    @asynccontextmanager
    async def factory():
        yield object()

    async def ticket_session(*args, **kwargs):
        return SimpleNamespace(config_json={"origin": "http://jhin.test"}), SimpleNamespace(
            kind="preview", config_json={"framework": "static"}
        )

    async def runner(*args):
        return {"status": 307, "headers": {"Location": location}, "body_base64": ""}

    app = FastAPI()
    install_preview_routes(app, factory, ticket_session, runner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://jhin.test"
    ) as client:
        response = await client.get(f"/runtime/previews/{sid}/valid/")
        assert response.status_code == 502
        assert "location" not in response.headers
