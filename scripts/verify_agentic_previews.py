"""Docker acceptance for isolated static, HTTP/WebSocket, Vite and Next previews.

Run in the runner image with this repository mounted read-only at /repo.
Only uniquely named preview-acceptance volumes/containers are created/removed.
The test manager never reaps the user's existing runner jobs.
"""
# Embedded fixture applications deliberately retain their original source layout.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import base64
import json
import runpy
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from jhin_sandbox_runner.jobs import JobManager
from jhin_sandbox_runner.preview_transport import _WS_SCRIPT, _execute_http
from jhin_sandbox_runner.sessions import SessionManager, SessionRecord, SessionRequest
from jhin_sandbox_runner.settings import Settings
from jhin_sandbox_runner.workspace_operations import WorkspaceOperation, run_operation

preview_command = runpy.run_path(
    "/repo/packages/connectors/src/jhin_connectors/cli/preview_adapters.py"
)["preview_command"]

ECHO_SERVER = r"""
import base64,hashlib,http.server,json,struct
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Upgrade","").lower()=="websocket":
            self.send_response(101);self.send_header("Upgrade","websocket");self.send_header("Connection","Upgrade")
            self.send_header("Sec-WebSocket-Accept",base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
            first=self.rfile.read(2);n=first[1]&127
            if n==126:n=struct.unpack('!H',self.rfile.read(2))[0]
            mask=self.rfile.read(4);data=self.rfile.read(n);data=bytes(b^mask[i%4] for i,b in enumerate(data))
            result=b'echo:'+data;self.wfile.write(bytes([129,len(result)])+result);self.wfile.flush();return
        data=b'<h1>HTTP preview</h1>';self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(data)
    def do_POST(self):
        data=self.rfile.read(int(self.headers.get('Content-Length','0')));self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'received':data.decode(),'cookie':self.headers.get('Cookie'),'authorization':self.headers.get('Authorization')}).encode())
http.server.ThreadingHTTPServer(('127.0.0.1',3000),Handler).serve_forever()
"""


async def main() -> None:
    settings = Settings(
        sandbox_docker_mode="desktop",
        sandbox_docker_socket=Path("/run/jhin/docker.sock"),
        sandbox_default_image="jhin-sandbox:agentic",
    )
    jobs = JobManager(settings)

    async def no_reaping() -> None:
        pass

    jobs.reap_orphans = no_reaping  # type: ignore[method-assign]
    await jobs.start()
    sessions = SessionManager(jobs, settings)
    workspaces = []
    checks = []

    async def start(framework: str, files: dict[str, str], custom: str = "") -> SessionRecord:
        key = "preview-acceptance-" + uuid4().hex
        workspaces.append(key)
        await run_operation(
            jobs,
            settings,
            key,
            WorkspaceOperation(
                operation="restore",
                args={
                    "files": [
                        {
                            "path": path,
                            "content_base64": base64.b64encode(content.encode()).decode(),
                            "expected_sha256": None,
                        }
                        for path, content in files.items()
                    ]
                },
            ),
        )
        sid = str(uuid4())
        record = await sessions.create(
            SessionRequest(
                session_id=sid,
                workspace_key=key,
                kind="preview",
                network="internet" if framework in {"vite", "next"} else "none",
                command=preview_command(framework, 3000, sid, custom),
                port=3000,
            )
        )
        return record

    async def request(
        record: SessionRecord, path: str = "/", method: str = "GET", body: bytes = b""
    ) -> dict[str, Any]:
        return await _execute_http(
            record,
            {
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body_base64": base64.b64encode(body).decode(),
            },
        )

    async def ready(record: SessionRecord, path: str, marker: bytes) -> bytes:
        last_notice = time.monotonic()
        async with asyncio.timeout(240):
            while True:
                if record.status != "running":
                    raise AssertionError(
                        f"Preview exited: {record.status}: {record.output[-5000:]}"
                    )
                try:
                    result = await request(record, path)
                    data = base64.b64decode(result["body_base64"])
                    if result["status"] == 200 and marker in data:
                        return data
                except HTTPException:
                    pass
                if time.monotonic() - last_notice > 20:
                    print(
                        json.dumps(
                            {"waiting_for": marker.decode(), "log_tail": record.output[-2500:]}
                        ),
                        flush=True,
                    )
                    last_notice = time.monotonic()
                await asyncio.sleep(1)

    async def websocket_echo(record: SessionRecord) -> None:
        process = await record.container.exec(
            cmd=[
                "python",
                "-I",
                "-u",
                "-c",
                _WS_SCRIPT,
                json.dumps({"path": "/socket", "protocols": []}),
                "3000",
            ],
            stdin=True,
            tty=False,
            user="1000:1000",
            workdir="/",
        )
        stream = process.start()
        await stream.write_in(b"")
        buffer = bytearray()

        async def line() -> Any:
            nonlocal buffer
            async with asyncio.timeout(25):
                while b"\n" not in buffer:
                    message = await stream.read_out()
                    if message is None:
                        raise AssertionError("WebSocket bridge ended")
                    buffer.extend(message.data)
                first, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                return json.loads(first)

        try:
            assert (await line())["type"] == "connected"
            await stream.write_in(
                (
                    json.dumps({"type": "text", "data": base64.b64encode(b"ping").decode()}) + "\n"
                ).encode()
            )
            response = await line()
            assert (
                response["type"] == "text" and base64.b64decode(response["data"]) == b"echo:ping"
            ), response
        finally:
            await stream.write_in(b'{"type":"close"}\n')
            await stream.close()

    try:
        if "--next-only" not in sys.argv:
            static = await start(
                "static",
                {
                    "index.html": "<h1>Static chart</h1><button onclick=\"this.textContent='Clicked'\">Click</button>"
                },
            )
            await ready(static, "/", b"Static chart")
            config = await static.container.show()
            assert not config["HostConfig"].get("PortBindings")
            assert config["HostConfig"]["ReadonlyRootfs"]
            checks.append("static HTML, private registered port, read-only root")
            print(json.dumps({"passed": checks[-1]}), flush=True)
            await sessions.stop(static.request.session_id)

            http = await start("http", {"server.py": ECHO_SERVER}, "python -I server.py")
            await ready(http, "/", b"HTTP preview")
            posted = await request(http, "/echo", "POST", b'{"hello":"world"}')
            decoded = json.loads(base64.b64decode(posted["body_base64"]))
            assert decoded == {
                "received": '{"hello":"world"}',
                "cookie": None,
                "authorization": None,
            }
            await websocket_echo(http)
            checks.append("HTTP POST and real WebSocket round trip")
            print(json.dumps({"passed": checks[-1]}), flush=True)
            await sessions.stop(http.request.session_id)

            vite = await start(
                "vite",
                {
                    "package.json": json.dumps(
                        {"type": "module", "dependencies": {"vite": "8.2.1"}}
                    ),
                    "index.html": '<h1>Vite preview</h1><script type="module" src="/main.js"></script>',
                    "main.js": "document.body.dataset.ready='yes';",
                },
            )
            stable = f"/runtime/previews/{vite.request.session_id}/"
            vitehtml = await ready(vite, stable, b"Vite preview")
            assert stable.encode() + b"@vite/client" in vitehtml
            module = await request(vite, stable + "main.js")
            assert module["status"] == 200 and b"dataset.ready" in base64.b64decode(
                module["body_base64"]
            )
            checks.append("Vite mounted HTML, module assets, dev server")
            print(json.dumps({"passed": checks[-1]}), flush=True)
            await sessions.stop(vite.request.session_id)

        nextapp = await start(
            "next",
            {
                "package.json": json.dumps(
                    {"dependencies": {"next": "16.3.1", "react": "19.2.8", "react-dom": "19.2.8"}}
                ),
                "app/layout.js": "export default function Layout({children}){return <html><body>{children}</body></html>}",
                "app/page.js": "export default function Page(){return <h1>Next preview</h1>}",
                "app/api/echo/route.js": "export async function POST(req){return Response.json({received:await req.json()})}",
            },
        )
        stable = f"/runtime/previews/{nextapp.request.session_id}"
        nexthtml = await ready(nextapp, stable, b"Next preview")
        assert (stable + "/_next/").encode() in nexthtml
        posted = await request(nextapp, stable + "/api/echo", "POST", b'{"hello":"next"}')
        assert json.loads(base64.b64decode(posted["body_base64"])) == {
            "received": {"hello": "next"}
        }
        checks.append("Next mounted page, chunk prefix and POST route")
        print(json.dumps({"passed": checks[-1]}), flush=True)
        await sessions.stop(nextapp.request.session_id)
        print(json.dumps({"passed": checks}, indent=2), flush=True)
    finally:
        await sessions.close()
        for key in workspaces:
            assert key.startswith("preview-acceptance-")
            await jobs.delete_workspace(key)
        await jobs.close()


asyncio.run(main())
