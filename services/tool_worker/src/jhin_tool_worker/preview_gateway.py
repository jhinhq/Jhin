"""Cookie-free, authenticated HTTP/WebSocket gateway for isolated app previews."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from jhin_connectors.cli.runner_client import runner_config

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_FRAME = 1024 * 1024
_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "range",
    "if-range",
    "next-router-state-tree",
    "next-router-prefetch",
    "next-url",
    "rsc",
}
_RESPONSE_HEADERS = {"content-type", "content-range", "accept-ranges", "content-language"}
_TEXT_TYPES = {
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/json",
    "text/x-component",
}


def checked_origin(origin: str | None, expected: str | None, own_origin: str) -> None:
    # Opaque sandbox requests use null. The DB capability, membership and
    # registered target are still verified for every one of those requests.
    if (
        origin is not None
        and origin != "null"
        and origin.rstrip("/") not in {str(expected).rstrip("/"), own_origin.rstrip("/")}
    ):
        raise HTTPException(403, "Preview origin is not permitted")


def preview_headers(prefix: str, origin: str) -> dict[str, str]:
    if not re.fullmatch(r"/[A-Za-z0-9/._-]+", prefix):
        prefix = "/runtime/previews"
    try:
        parsed_origin = urlsplit(origin)
    except ValueError:
        parsed_origin = urlsplit("http://invalid.local")
    if (
        any(ord(char) < 33 or ord(char) == 127 for char in origin)
        or parsed_origin.scheme not in {"http", "https"}
        or not re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", parsed_origin.netloc)
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        origin = "http://invalid.local"
    else:
        origin = urlunsplit((parsed_origin.scheme, parsed_origin.netloc, "", "", ""))
    absolute = origin.rstrip("/") + prefix + "/"
    websocket = absolute.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    csp = (
        "sandbox allow-scripts allow-forms allow-downloads; default-src 'none'; "
        f"script-src 'unsafe-inline' 'unsafe-eval' {absolute}; "
        f"style-src 'unsafe-inline' {absolute}; img-src data: blob: {absolute}; "
        f"font-src data: {absolute}; connect-src {absolute} {websocket}; "
        f"form-action {absolute}; frame-ancestors 'self'; base-uri 'none'; object-src 'none'"
    )
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "X-Frame-Options": "SAMEORIGIN",
        "Access-Control-Allow-Origin": "null",
        "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": (
            "content-type, range, next-router-state-tree, next-router-prefetch, next-url, rsc"
        ),
        "Vary": "Origin",
    }


def upstream_path(path: str, query: str, session_id: UUID, framework: str) -> str:
    if "\\" in path or any(ord(char) < 32 for char in path) or path.startswith("/"):
        raise HTTPException(422, "Invalid preview path")
    stable = f"/runtime/previews/{session_id}"
    target = f"{stable}/{path}" if framework in {"vite", "next"} else "/" + path
    if framework == "next" and not path:
        target = stable
    return target + ("?" + query if query else "")


def scoped_redirect(location: str, prefix: str, session_id: UUID) -> str:
    invalid = "Preview redirects must remain inside the preview"
    if len(location) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in location):
        raise HTTPException(502, invalid)
    try:
        parsed = urlsplit(location)
    except ValueError as exc:
        raise HTTPException(502, invalid) from exc
    if parsed.scheme or parsed.netloc or not location.startswith("/"):
        raise HTTPException(502, invalid)
    decoded = parsed.path
    for _ in range(16):
        if (
            "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))
        ):
            raise HTTPException(502, invalid)
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        raise HTTPException(502, invalid)
    stable = f"/runtime/previews/{session_id}"
    path = parsed.path
    if path == stable or path.startswith(stable + "/"):
        path = path[len(stable) :] or "/"
    return urlunsplit(("", "", prefix + path, parsed.query, parsed.fragment))


def _bootstrap(prefix: str) -> str:
    # Patching browser request constructors supports simple HTTP applications
    # whose source uses absolute fetch/WebSocket paths. Module imports and
    # static attributes are rewritten in their source responses below. Some
    # frameworks read document.cookie while hydrating; opaque documents throw
    # on that native getter. Present an empty, non-persistent cookie facade
    # before app scripts run, without consulting cookies or changing origins.
    return (
        "<script>"
        + r"""(()=>{const p=PREFIX;
const cookies={get:()=>"",set:()=>{},enumerable:true,configurable:false};
try{Object.defineProperty(document,"cookie",cookies)}catch{
try{Object.defineProperty(Object.getPrototypeOf(document),"cookie",cookies)}catch{}}
const route=v=>{
if(typeof v!=="string")return v;
if(v.startsWith(p+"/")||v===p)return v;
if(v.startsWith("/")&&!v.startsWith("//"))return p+v;
try{const u=new URL(v,location.href);
if((u.protocol==="ws:"||u.protocol==="wss:")&&
[location.hostname,"localhost","127.0.0.1"].includes(u.hostname)){
return (location.protocol==="https:"?"wss:":"ws:")+"//"+location.host+p+
(u.pathname.startsWith(p)?u.pathname.slice(p.length):u.pathname)+u.search;
}}catch{}return v};
const f=window.fetch;
window.fetch=(v,i)=>f(typeof v==="string"?route(v):v,{...i,credentials:"omit"});
const W=window.WebSocket;
window.WebSocket=class extends W{constructor(u,p){super(route(String(u)),p)}};
const x=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(m,u,...a){return x.call(this,m,route(String(u)),...a)};
for(const k of ["pushState","replaceState"]){const q=history[k];
history[k]=function(s,t,u){return q.call(this,s,t,u==null?u:route(String(u)))}}
})();""".replace("PREFIX", json.dumps(prefix))
        + "</script>"
    )


def rewrite_preview_body(body: bytes, mime_type: str, prefix: str, session_id: UUID) -> bytes:
    mime = mime_type.split(";", 1)[0].strip().lower()
    if mime not in _TEXT_TYPES:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    stable = f"/runtime/previews/{session_id}"
    # Stable framework base paths are replaced as complete prefixes, preventing
    # ticket stacking when a string already contains the current capability.
    suffix = prefix[len(stable) :]
    text = re.sub(
        re.escape(stable) + r"(?!" + re.escape(suffix) + r")(?=/|[\"'`?])",
        lambda match: prefix,
        text,
    )
    escaped_stable = stable.replace("/", r"\/")
    text = text.replace(escaped_stable, prefix.replace("/", r"\/"))
    if mime == "text/html":

        def attribute(match: re.Match[str]) -> str:
            start, path = match.group(1), match.group(2)
            return start + (path if path.startswith(prefix) else prefix + path)

        text = re.sub(
            r'((?:src|href|action|poster)\s*=\s*["\'])(/(?!/)[^"\']*)', attribute, text, flags=re.I
        )
        bootstrap = _bootstrap(prefix)
        head = re.search(r"<head(?:\s[^>]*)?>", text, flags=re.I)
        text = text[: head.end()] + bootstrap + text[head.end() :] if head else bootstrap + text
    elif mime in {"text/javascript", "application/javascript"}:
        # Static ES imports need rewriting before the browser evaluates them.
        text = re.sub(
            r'((?:from\s*|import\s*\(?|export\s+[^;]*?from\s*)["\'])(/(?!/)[^"\']*)',
            lambda m: (
                m.group(1) + (m.group(2) if m.group(2).startswith(prefix) else prefix + m.group(2))
            ),
            text,
        )
    elif mime == "text/css":
        text = re.sub(
            r'(url\(\s*["\']?)(/(?!/)[^\)"\']*)',
            lambda m: (
                m.group(1) + (m.group(2) if m.group(2).startswith(prefix) else prefix + m.group(2))
            ),
            text,
        )
    return text.encode("utf-8")


def install_preview_routes(
    app: FastAPI, factory: Any, ticket_session: Any, runner_request: Any
) -> None:
    @app.middleware("http")
    async def isolate_preview_responses(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/runtime/previews/"):
            parts = request.url.path.split("/")
            prefix = "/".join(parts[:5]) if len(parts) >= 5 else "/runtime/previews"
            # Rewrites may replace Host with the private Docker service name.
            # The authenticated ticket records the browser's public origin;
            # never derive a wider policy from untrusted forwarded headers.
            origin = getattr(request.state, "preview_origin", str(request.base_url).rstrip("/"))
            for name, value in preview_headers(prefix, origin).items():
                response.headers[name] = value
            for name in ("set-cookie", "www-authenticate", "proxy-authenticate", "refresh"):
                if name in response.headers:
                    del response.headers[name]
        return response

    @app.api_route(
        "/runtime/previews/{session_id}/{ticket}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    @app.api_route(
        "/runtime/previews/{session_id}/{ticket}/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def preview_http(
        request: Request, session_id: UUID, ticket: str, path: str = ""
    ) -> Response:
        async with factory() as db:
            grant, parent = await ticket_session(
                db, session_id, ticket, origin=request.headers.get("origin")
            )
            if parent.kind != "preview":
                raise HTTPException(403, "This session is not a preview")
            checked_origin(
                request.headers.get("origin"),
                grant.config_json.get("origin"),
                str(request.base_url).rstrip("/"),
            )
            request.state.preview_origin = grant.config_json.get("origin") or str(
                request.base_url
            ).rstrip("/")
            framework = parent.config_json.get("framework", "static")
        if not path and not request.url.path.endswith("/"):
            # Keep relative assets below this capability and never expose an
            # internal hostname through Starlette's automatic slash redirect.
            root_location = request.url.path + "/"
            if request.url.query:
                root_location += "?" + request.url.query
            return Response(status_code=307, headers={"Location": root_location})
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise HTTPException(413, "Preview body exceeds 2 MiB")
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            return Response(status_code=204)
        target = upstream_path(path, request.url.query, session_id, framework)
        result = await runner_request(
            "POST",
            f"/v1/sessions/{session_id}/preview/http",
            {
                "method": request.method,
                "path": target,
                "headers": {
                    name: value
                    for name, value in request.headers.items()
                    if name.lower() in _REQUEST_HEADERS
                },
                "body_base64": base64.b64encode(data).decode(),
            },
        )
        encoded = result.get("body_base64", "")
        if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE * 4 // 3 + 4:
            raise HTTPException(502, "Preview response exceeds limit")
        try:
            body = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise HTTPException(502, "Invalid preview response") from exc
        status = result.get("status", 502)
        if type(status) is not int or not 200 <= status <= 599:
            raise HTTPException(502, "Invalid preview status")
        prefix = f"/runtime/previews/{session_id}/{ticket}"
        headers = {
            name.lower(): value
            for name, value in result.get("headers", {}).items()
            if name.lower() in _RESPONSE_HEADERS
        }
        location = next(
            (v for k, v in result.get("headers", {}).items() if k.lower() == "location"), None
        )
        if location:
            headers["location"] = scoped_redirect(location, prefix, session_id)
        body = rewrite_preview_body(body, headers.get("content-type", ""), prefix, session_id)
        return Response(body, status_code=status, headers=headers)

    @app.websocket("/runtime/previews/{session_id}/{ticket}/{path:path}")
    async def preview_socket(socket: WebSocket, session_id: UUID, ticket: str, path: str) -> None:
        accepted = False
        upstream = None
        try:
            async with factory() as db:
                grant, parent = await ticket_session(
                    db, session_id, ticket, origin=socket.headers.get("origin")
                )
                if parent.kind != "preview":
                    raise HTTPException(403, "Not a preview")
                origin = (
                    ("https" if socket.url.scheme == "wss" else "http") + "://" + socket.url.netloc
                )
                checked_origin(
                    socket.headers.get("origin"), grant.config_json.get("origin"), origin
                )
                target = upstream_path(
                    path,
                    socket.url.query,
                    session_id,
                    parent.config_json.get("framework", "static"),
                )
            base, runner_token = runner_config()
            parsed = urlsplit(base)
            runner_url = urlunsplit(
                (
                    "wss" if parsed.scheme == "https" else "ws",
                    parsed.netloc,
                    f"/v1/sessions/{session_id}/preview/ws",
                    "",
                    "",
                )
            )
            async with connect(
                runner_url,
                additional_headers={"Authorization": f"Bearer {runner_token}"},
                max_size=MAX_FRAME * 2,
                proxy=None,
                compression=None,
            ) as upstream:
                await upstream.send(
                    json.dumps({"path": target, "protocols": socket.scope.get("subprotocols", [])})
                )
                ready = json.loads(await asyncio.wait_for(upstream.recv(), timeout=25))
                if ready.get("type") != "connected":
                    raise HTTPException(502, "Preview WebSocket is not ready")
                protocol = ready.get("protocol")
                if protocol and protocol not in socket.scope.get("subprotocols", []):
                    raise HTTPException(502, "Invalid preview WebSocket protocol")
                await socket.accept(subprotocol=protocol)
                accepted = True

                async def to_browser() -> None:
                    async for raw in upstream:
                        message = json.loads(raw)
                        if message.get("type") in {"closed", "error"}:
                            return
                        if message.get("type") not in {"text", "bytes"}:
                            continue
                        data = base64.b64decode(message["data"], validate=True)
                        if len(data) > MAX_FRAME:
                            raise HTTPException(413, "Preview frame exceeds limit")
                        if message["type"] == "text":
                            await socket.send_text(data.decode("utf-8"))
                        else:
                            await socket.send_bytes(data)

                async def to_runner() -> None:
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        data = message.get("bytes")
                        kind = "bytes"
                        if data is None:
                            data, kind = (message.get("text") or "").encode("utf-8"), "text"
                        if len(data) > MAX_FRAME:
                            raise HTTPException(413, "Preview frame exceeds limit")
                        await upstream.send(
                            json.dumps({"type": kind, "data": base64.b64encode(data).decode()})
                        )

                async def validity() -> None:
                    while True:
                        await asyncio.sleep(5)
                        async with factory() as db:
                            await ticket_session(
                                db, session_id, ticket, origin=socket.headers.get("origin")
                            )

                tasks = [
                    asyncio.create_task(to_browser()),
                    asyncio.create_task(to_runner()),
                    asyncio.create_task(validity()),
                ]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (
            WebSocketDisconnect,
            WebSocketException,
            HTTPException,
            ValueError,
            OSError,
            TimeoutError,
        ):
            pass
        finally:
            with contextlib.suppress(Exception):
                await socket.close(code=1000 if accepted else 1008)
