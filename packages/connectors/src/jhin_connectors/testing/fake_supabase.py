"""Deterministic, stdlib-only Supabase Management API fake."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_TOKEN = "fake-supabase-token"
DEFAULT_PROJECT_REF = "abcdefghijklmnopqrst"
_MUTATIONS = frozenset({"deploy", "delete"})


@dataclass(frozen=True)
class _FakeResponse:
    status: int
    payload: dict[str, Any] | list[Any] | None
    drop_connection: bool = False


class FakeSupabaseState:
    """In-memory Management API resources and request inspection state."""

    def __init__(
        self,
        *,
        token: str = DEFAULT_TOKEN,
        project_ref: str = DEFAULT_PROJECT_REF,
    ) -> None:
        self.token = token
        self.project_ref = project_ref
        self.lock = threading.RLock()
        self.redirects: dict[str, str] = {}
        self.response_delays: dict[str, float] = {}
        self.response_delay_started = threading.Event()
        self.response_delay_release = threading.Event()
        self.ignore_log_limit = False
        self.log_error: Any = None
        self.deploy_response_slug: str | None = None
        self._seed()

    def _seed(self) -> None:
        self.project: dict[str, Any] = {
            "id": "project-id-1",
            "ref": self.project_ref,
            "organization_id": "organization-id-1",
            "organization_slug": "fake-organization",
            "name": "Fake Supabase Project",
            "region": "us-west-1",
            "created_at": "2026-08-17T00:00:00Z",
            "status": "ACTIVE_HEALTHY",
            "database": {
                "host": "must-never-be-returned",
                "version": "17",
                "providerSecret": "database-provider-secret",
            },
            "providerSecret": "project-provider-secret",
        }
        self.functions: dict[str, dict[str, Any]] = {
            "hello-world": self._function("hello-world", version=1),
            "scheduled-cleanup": self._function("scheduled-cleanup", version=3),
        }
        self.logs: list[dict[str, Any]] = [
            {
                "timestamp": "2026-08-17T00:30:00Z",
                "source": "edge_logs",
                "event_message": "GET /widgets completed",
                "path": "/widgets",
                "status_code": 200,
                "method": "GET",
                "providerSecret": "log-provider-secret",
            },
            {
                "timestamp": "2026-08-17T00:20:00Z",
                "source": "edge_logs",
                "event_message": "POST /widgets completed",
                "path": "/widgets",
                "status_code": 201,
                "method": "POST",
            },
        ]
        self.counters = dict.fromkeys(_MUTATIONS, 0)
        self.last_requests: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: set[str] = set()
        self.log_error = None
        self.deploy_response_slug = None

    @staticmethod
    def _function(slug: str, *, version: int) -> dict[str, Any]:
        return {
            "id": f"function-{slug}",
            "slug": slug,
            "name": slug,
            "status": "ACTIVE",
            "version": version,
            "created_at": 1_700_000_000_000,
            "updated_at": 1_700_000_010_000,
            "verify_jwt": True,
            "import_map": False,
            "entrypoint_path": "index.ts",
            "import_map_path": "",
            "ezbr_sha256": "provider-digest",
            "providerSecret": "function-provider-secret",
            "source": "source-secret-marker",
        }

    def authorized(self, authorization: str | None) -> bool:
        return authorization == f"Bearer {self.token}"

    def record_request(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        header_names: list[str],
        content_type: str,
        body: dict[str, Any],
        metadata: dict[str, Any] | None,
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "method": method,
            "path": path,
            "query": {key: list(values) for key, values in query.items()},
            "header_names": list(header_names),
            "content_type": content_type,
            "body": json.loads(json.dumps(body)),
            "metadata": json.loads(json.dumps(metadata)) if metadata is not None else None,
            "files": json.loads(json.dumps(files)),
        }
        with self.lock:
            self.requests.append(record)
        return record

    def requests_for(self, method: str, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return cast(
                "list[dict[str, Any]]",
                json.loads(
                    json.dumps(
                        [
                            request
                            for request in self.requests
                            if request["method"] == method and request["path"] == path
                        ]
                    )
                ),
            )

    def seed_many_functions(self, count: int) -> None:
        with self.lock:
            for index in range(count):
                slug = f"bulk-function-{index:04d}"
                self.functions[slug] = self._function(slug, version=index + 1)

    def arm_fault(self, mutation: str) -> bool:
        if mutation not in _MUTATIONS:
            return False
        with self.lock:
            self.faults.add(mutation)
        return True

    def record_mutation(self, mutation: str, request: dict[str, Any]) -> bool:
        with self.lock:
            self.counters[mutation] += 1
            self.last_requests[mutation] = json.loads(json.dumps(request))
            should_drop = mutation in self.faults
            self.faults.discard(mutation)
            return should_drop

    def reset(self) -> None:
        with self.lock:
            self.redirects.clear()
            self.response_delays.clear()
            self.response_delay_started.clear()
            self.response_delay_release.clear()
            self.ignore_log_limit = False
            self._seed()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return cast(
                "dict[str, Any]",
                json.loads(
                    json.dumps(
                        {
                            "counters": self.counters,
                            "last_requests": self.last_requests,
                            "requests": self.requests,
                        }
                    )
                ),
            )


def _multipart_body(
    content_type: str,
    raw: bytes,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not content_type.casefold().startswith("multipart/form-data;"):
        return None, []
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("ascii", errors="ignore")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + raw
    )
    if not message.is_multipart():
        return None, []
    metadata: dict[str, Any] | None = None
    files: list[dict[str, Any]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        decoded_payload = part.get_payload(decode=True)
        payload = decoded_payload if isinstance(decoded_payload, bytes) else b""
        if name == "metadata":
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                metadata = decoded
        elif name == "file":
            filename = part.get_filename()
            if isinstance(filename, str):
                files.append({"filename": filename, "size": len(payload)})
    return metadata, files


def _query_limit(sql: str, default: int = 100) -> int:
    match = re.search(r"\nLIMIT ([0-9]+)\Z", sql)
    if match is None:
        return default
    try:
        return max(1, min(200, int(match.group(1), 10)))
    except ValueError:
        return default


def handle_request(
    state: FakeSupabaseState,
    *,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    header_names: list[str],
    content_type: str,
    body: dict[str, Any],
    metadata: dict[str, Any] | None,
    files: list[dict[str, Any]],
) -> _FakeResponse:
    """Pure-ish router shared by the threaded server and direct tests."""
    if method == "GET" and path == "/_state":
        return _FakeResponse(200, state.snapshot())
    if method == "POST" and path == "/_reset":
        state.reset()
        return _FakeResponse(200, {"ok": True})
    if method == "POST" and path == "/_fault":
        mutation = body.get("mutation")
        if not isinstance(mutation, str) or not state.arm_fault(mutation):
            return _FakeResponse(400, {"error": "unknown mutation"})
        return _FakeResponse(200, {"armed": mutation})

    request = state.record_request(
        method=method,
        path=path,
        query=query,
        header_names=header_names,
        content_type=content_type,
        body=body,
        metadata=metadata,
        files=files,
    )
    delay = state.response_delays.get(path, 0.0)
    if delay > 0:
        state.response_delay_started.set()
        state.response_delay_release.wait(timeout=delay)
    redirect = state.redirects.get(path)
    if redirect is not None:
        return _FakeResponse(302, None)
    if not state.authorized(headers.get("Authorization")):
        return _FakeResponse(401, {"error": "unauthorized"})

    project_match = re.fullmatch(r"/v1/projects/([^/]+)", path)
    if method == "GET" and project_match:
        requested_ref = unquote(project_match.group(1))
        if requested_ref != state.project_ref:
            return _FakeResponse(404, {"error": "not found"})
        with state.lock:
            payload = json.loads(json.dumps(state.project))
        return _FakeResponse(200, payload)

    logs_match = re.fullmatch(r"/v1/projects/([^/]+)/analytics/endpoints/logs", path)
    if method == "GET" and logs_match:
        requested_ref = unquote(logs_match.group(1))
        if requested_ref != state.project_ref:
            return _FakeResponse(404, {"error": "not found"})
        sql = query.get("sql", [""])[0]
        limit = _query_limit(sql)
        with state.lock:
            selected = state.logs if state.ignore_log_limit else state.logs[:limit]
            result = json.loads(json.dumps(selected))
            error = state.log_error
        return _FakeResponse(200, {"result": result, "error": error})

    functions_match = re.fullmatch(r"/v1/projects/([^/]+)/functions", path)
    if method == "GET" and functions_match:
        requested_ref = unquote(functions_match.group(1))
        if requested_ref != state.project_ref:
            return _FakeResponse(404, {"error": "not found"})
        with state.lock:
            payload = [
                json.loads(json.dumps(function)) for _, function in sorted(state.functions.items())
            ]
        return _FakeResponse(200, payload)

    deploy_match = re.fullmatch(r"/v1/projects/([^/]+)/functions/deploy", path)
    if method == "POST" and deploy_match:
        requested_ref = unquote(deploy_match.group(1))
        slug = query.get("slug", [""])[0]
        if requested_ref != state.project_ref or not slug or metadata is None or not files:
            return _FakeResponse(400, {"error": "invalid deploy"})
        with state.lock:
            deployed = state._function(slug, version=state.counters["deploy"] + 1)
            deployed["name"] = str(metadata.get("name", slug))
            deployed["verify_jwt"] = metadata.get("verify_jwt")
            deployed["entrypoint_path"] = str(metadata.get("entrypoint_path", ""))
            state.functions[slug] = deployed
            response = dict(deployed)
            if state.deploy_response_slug is not None:
                response["slug"] = state.deploy_response_slug
        drop = state.record_mutation("deploy", request)
        return _FakeResponse(201, response, drop_connection=drop)

    delete_match = re.fullmatch(r"/v1/projects/([^/]+)/functions/([^/]+)", path)
    if method == "DELETE" and delete_match:
        requested_ref = unquote(delete_match.group(1))
        slug = unquote(delete_match.group(2))
        if requested_ref != state.project_ref:
            return _FakeResponse(404, {"error": "not found"})
        with state.lock:
            if slug not in state.functions:
                return _FakeResponse(404, {"error": "not found"})
            del state.functions[slug]
        drop = state.record_mutation("delete", request)
        return _FakeResponse(200, {}, drop_connection=drop)

    return _FakeResponse(404, {"error": "not found"})


def _make_handler(state: FakeSupabaseState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"), 10)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if 0 < length <= 131_072 else b""
            content_type = self.headers.get("Content-Type", "")
            metadata, files = _multipart_body(content_type, raw)
            body: dict[str, Any] = {}
            if content_type.casefold().startswith("application/json") and raw:
                try:
                    decoded = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    decoded = {}
                if isinstance(decoded, dict):
                    body = decoded
            split = urlsplit(self.path)
            result = handle_request(
                state,
                method=method,
                path=split.path,
                query=parse_qs(split.query, keep_blank_values=True),
                headers={"Authorization": self.headers.get("Authorization", "")},
                header_names=sorted(name.casefold() for name in self.headers),
                content_type=content_type,
                body=body,
                metadata=metadata,
                files=files,
            )
            if result.drop_connection:
                self.close_connection = True
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if result.status == 302:
                data = b""
                self.send_response(302)
                self.send_header(
                    "Location", state.redirects.get(split.path, "https://invalid.example")
                )
            else:
                data = b"" if result.payload is None else json.dumps(result.payload).encode()
                self.send_response(result.status)
                self.send_header("Content-Type", "application/json")
            self.close_connection = True
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(data)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


class FakeSupabaseServer:
    """Threaded fake Management API usable as a pytest context manager."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        token: str = DEFAULT_TOKEN,
        project_ref: str = DEFAULT_PROJECT_REF,
    ) -> None:
        self.state = FakeSupabaseState(token=token, project_ref=project_ref)
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}"

    def __enter__(self) -> FakeSupabaseServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    port = int(os.environ.get("FAKE_SUPABASE_PORT", "8080"))
    state = FakeSupabaseState(
        token=os.environ.get("FAKE_SUPABASE_TOKEN", DEFAULT_TOKEN),
        project_ref=os.environ.get("FAKE_SUPABASE_PROJECT_REF", DEFAULT_PROJECT_REF),
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    print(f"fake Supabase Management API listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
