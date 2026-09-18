"""Proxy only a registered preview's loopback port, using bounded Docker exec.

There are no published ports and no route to a caller-selected host. Trusted
Python runs in isolated mode in the preview container; site/user imports and
proxy environment variables cannot redirect the transport.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_FRAME = 1024 * 1024
_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "range",
    "if-range",
    "if-none-match",
    "if-modified-since",
    "next-router-state-tree",
    "next-router-prefetch",
    "next-url",
    "rsc",
}


def forwarded_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in _REQUEST_HEADERS
        and len(value) < 16_384
        and "\r" not in value
        and "\n" not in value
    }


def validate_preview_request(payload: dict[str, Any]) -> dict[str, Any]:
    method = payload.get("method", "GET")
    path = payload.get("path", "/")
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        raise HTTPException(405, "Preview method is not supported")
    if (
        not isinstance(path, str)
        or len(path) > 16_384
        or not path.startswith("/")
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or urlsplit(path).netloc
        or urlsplit(path).scheme
    ):
        raise HTTPException(422, "Preview path must remain on its registered server")
    encoded = payload.get("body_base64", "")
    if not isinstance(encoded, str) or len(encoded) > MAX_BODY * 4 // 3 + 4:
        raise HTTPException(413, "Preview request body exceeds 2 MiB")
    try:
        if len(base64.b64decode(encoded, validate=True)) > MAX_BODY:
            raise ValueError("too large")
    except ValueError as exc:
        raise HTTPException(422, "Invalid preview request body") from exc
    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        raise HTTPException(422, "Invalid preview headers")
    return {
        "method": method,
        "path": path,
        "headers": forwarded_headers(headers),
        "body_base64": encoded,
    }


_HTTP_SCRIPT = r"""
import base64,http.client,json,sys
p=json.loads(sys.argv[1]); port=int(sys.argv[2])
try:
    c=http.client.HTTPConnection("127.0.0.1",port,timeout=20)
    headers=p["headers"]; headers["Accept-Encoding"]="identity"
    headers["Host"]="127.0.0.1:"+str(port)
    c.request(p["method"],p["path"],body=base64.b64decode(p["body_base64"]),headers=headers)
    r=c.getresponse(); data=r.read(8388609)
    if len(data)>8388608: raise ValueError("Preview response exceeds 8 MiB")
    print(json.dumps({"status":r.status,"headers":dict(r.getheaders()),"body_base64":base64.b64encode(data).decode()}))
    c.close()
except Exception:
    print(json.dumps({"error":"Preview server is not ready or exceeded its response limit"}))
"""

_WS_SCRIPT = r"""
import base64,json,sys,threading
import websocket
p=json.loads(sys.argv[1]); port=int(sys.argv[2]); lock=threading.Lock()
def emit(v):
    with lock: print(json.dumps(v),flush=True)
try:
    ws=websocket.create_connection("ws://127.0.0.1:"+str(port)+p["path"],timeout=20,origin="http://127.0.0.1:"+str(port),subprotocols=p.get("protocols",[]),http_proxy_host=None)
    emit({"type":"connected","protocol":ws.getsubprotocol()})
    def reader():
        try:
            while True:
                opcode,data=ws.recv_data(control_frame=True)
                if opcode==8: break
                if opcode not in (1,2): continue
                if isinstance(data,str): data=data.encode()
                if len(data)>1048576: break
                kind="text" if opcode==1 else "bytes"
                emit({"type":kind,"data":base64.b64encode(data).decode()})
        except Exception: pass
        emit({"type":"closed"})
    threading.Thread(target=reader,daemon=True).start()
    for line in sys.stdin:
        p=json.loads(line); data=base64.b64decode(p.get("data",""),validate=True)
        if len(data)>1048576: break
        if p.get("type")=="text": ws.send(data.decode(),opcode=1)
        elif p.get("type")=="bytes": ws.send(data,opcode=2)
        else: break
    ws.close()
except Exception:
    emit({"type":"error","message":"Preview WebSocket could not connect"})
"""


def preview_record(manager: Any, session_id: str) -> Any:
    record = manager.get(session_id)
    if record.request.kind != "preview" or record.status != "running":
        raise HTTPException(409, "Preview is not running")
    return record


async def _execute_http(record: Any, payload: dict[str, Any]) -> dict[str, Any]:
    process = await record.container.exec(
        cmd=["python", "-I", "-c", _HTTP_SCRIPT, json.dumps(payload), str(record.request.port)],
        stdin=False,
        tty=False,
        user="1000:1000",
        workdir="/",
    )
    stream = process.start()
    data = bytearray()
    try:
        async with asyncio.timeout(25):
            while message := await stream.read_out():
                data.extend(message.data)
                if len(data) > MAX_RESPONSE * 4 // 3 + 65_536:
                    raise HTTPException(502, "Preview response exceeded transport limit")
    finally:
        with contextlib.suppress(Exception):
            await stream.close()
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(502, "Preview transport returned invalid data") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "Preview transport returned invalid data")
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return dict(result)


def install_runner_preview_routes(app: Any, manager: Any, require_token: Any) -> None:
    router = APIRouter(prefix="/v1/sessions")
    capacity = asyncio.Semaphore(16)

    @router.post("/{session_id}/preview/http", dependencies=[Depends(require_token)])
    async def preview_http(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = validate_preview_request(payload)
        record = preview_record(manager, session_id)
        async with capacity:
            return await _execute_http(record, data)

    @router.websocket("/{session_id}/preview/ws")
    async def preview_websocket(socket: WebSocket, session_id: str) -> None:
        # require_token's Request annotation is HTTP-specific; validate the
        # same header explicitly for websocket scopes before accepting.
        try:
            require_token(socket)
            record = preview_record(manager, session_id)
        except HTTPException:
            await socket.close(code=1008)
            return
        stream = None
        process = None
        await socket.accept()
        try:
            initial = await asyncio.wait_for(socket.receive_json(), timeout=10)
            data = validate_preview_request({"path": initial.get("path", "/")})
            protocols = initial.get("protocols", [])
            if (
                not isinstance(protocols, list)
                or len(protocols) > 8
                or any(
                    not isinstance(p, str)
                    or not p
                    or len(p) > 128
                    or any(ord(c) < 33 or ord(c) > 126 for c in p)
                    for p in protocols
                )
            ):
                raise HTTPException(422, "Invalid preview WebSocket protocols")
            async with capacity:
                process = await record.container.exec(
                    cmd=[
                        "python",
                        "-I",
                        "-u",
                        "-c",
                        _WS_SCRIPT,
                        json.dumps({"path": data["path"], "protocols": protocols}),
                        str(record.request.port),
                    ],
                    stdin=True,
                    tty=False,
                    user="1000:1000",
                    workdir="/",
                )
                stream = process.start()
                await stream.write_in(b"")

                async def upstream() -> None:
                    buffer = bytearray()
                    while message := await stream.read_out():
                        buffer.extend(message.data)
                        if len(buffer) > MAX_FRAME * 4 // 3 + 65_536:
                            raise HTTPException(413, "Preview frame exceeds limit")
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            await socket.send_json(json.loads(line))

                async def downstream() -> None:
                    while True:
                        message = await socket.receive_text()
                        if len(message) > MAX_FRAME * 4 // 3 + 2048:
                            raise HTTPException(413, "Preview frame exceeds limit")
                        value = json.loads(message)
                        if value.get("type") not in {"text", "bytes", "close"}:
                            raise HTTPException(422, "Invalid preview frame")
                        await stream.write_in((json.dumps(value) + "\n").encode())

                tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (TimeoutError, WebSocketDisconnect, HTTPException, ValueError):
            pass
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.write_in(b'{"type":"close"}\n')
                    await stream.close()
            with contextlib.suppress(Exception):
                await socket.close()

    app.include_router(router)
