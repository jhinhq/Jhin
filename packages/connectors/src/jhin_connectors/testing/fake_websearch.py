"""Deterministic, stdlib-only web search + page double for dev and tests.

One server speaks all three search-backend wire shapes of the web connector
(docs/architecture/web.md) and serves a few fetchable HTML pages so
``web.fetch`` can be exercised with zero real credentials:

- ``POST /search`` — Tavily shape (``Authorization: Bearer``, ``max_results``)
  or Exa shape when an ``x-api-key`` header is present (``numResults``);
- ``GET /res/v1/web/search`` — Brave shape (``X-Subscription-Token``,
  ``q``/``count`` query parameters);
- ``GET /pages/…`` — HTML pages (plus a huge one, a binary one, a
  same-origin redirect, and a cross-origin redirect) whose URLs the search
  results point at;
- ``GET /_state`` / ``POST /_reset`` — inspection hooks for tests.

Runs as a pytest helper (``FakeWebSearchServer``) or as the
``fake-websearch`` compose service (``python -m
jhin_connectors.testing.fake_websearch``).
"""

from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_TOKEN = "fake-websearch-token"

# A 1x1 transparent PNG.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

_PAGE_JHIN = (
    "<!doctype html><html><head><title>About Jhin</title>"
    "<style>body { font-family: sans-serif; }</style>"
    '<script>console.log("this script text must never reach extracted output");</script>'
    "</head><body><h1>Jhin</h1>"
    "<p>Jhin is a self-hosted platform for hierarchical teams of autonomous AI agents.</p>"
    "<p>Jhin agents get internet access through deny-by-default connectors.</p>"
    "<ul><li>web.search finds pages</li><li>web.fetch reads them</li></ul>"
    "<noscript>hidden noscript content</noscript></body></html>"
)
_PAGE_CHANGELOG = (
    "<!doctype html><html><head><title>Jhin Changelog</title></head><body>"
    "<h1>Changelog</h1><p>Version 0.1.0 added provider-independent web search.</p>"
    "<p>Version 0.0.9 added the generic HTTP connector.</p></body></html>"
)
_PAGE_HUGE = (
    "<!doctype html><html><head><title>Huge page</title></head><body><p>"
    + ("web content padding sentence for truncation tests. " * 900)
    + "</p></body></html>"
)


class FakeWebSearchState:
    """Search/fetch counters plus the queries seen, for test assertions."""

    def __init__(self, *, token: str = DEFAULT_TOKEN) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.searches: dict[str, int] = {"tavily": 0, "brave": 0, "exa": 0}
        self.queries: list[str] = []
        self.page_fetches: int = 0

    def record_search(self, backend: str, query: str) -> None:
        with self.lock:
            self.searches[backend] = self.searches.get(backend, 0) + 1
            self.queries.append(query)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "searches": dict(self.searches),
                "queries": list(self.queries),
                "page_fetches": self.page_fetches,
            }

    def reset(self) -> None:
        with self.lock:
            self.searches = {"tavily": 0, "brave": 0, "exa": 0}
            self.queries.clear()
            self.page_fetches = 0


def _result_rows(host: str, query: str, limit: int) -> list[dict[str, str]]:
    """Deterministic hits pointing at this server's own fetchable pages."""
    base = f"http://{host}"
    rows = [
        {
            "title": f"About Jhin — result for '{query}'",
            "url": f"{base}/pages/jhin",
            "snippet": f"Jhin platform overview matching '{query}'.",
            "published": "2026-08-01",
        },
        {
            "title": "Jhin Changelog",
            "url": f"{base}/pages/changelog",
            "snippet": "Version 0.1.0 added provider-independent web search.",
            "published": "2026-08-15",
        },
        {
            "title": "Huge page",
            "url": f"{base}/pages/huge",
            "snippet": "A very large page for truncation tests.",
            "published": "2026-07-01",
        },
    ]
    return rows[: max(1, min(limit, len(rows)))]


def _tavily_payload(rows: list[dict[str, str]], query: str) -> dict[str, Any]:
    return {
        "query": query,
        "results": [
            {
                "title": row["title"],
                "url": row["url"],
                "content": row["snippet"],
                "published_date": row["published"],
                "score": 0.9,
            }
            for row in rows
        ],
    }


def _brave_payload(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "type": "search",
        "web": {
            "results": [
                {
                    "title": row["title"],
                    "url": row["url"],
                    "description": row["snippet"],
                    "page_age": row["published"],
                }
                for row in rows
            ]
        },
    }


def _exa_payload(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "requestId": "fake-exa-request",
        "results": [
            {
                "title": row["title"],
                "url": row["url"],
                "text": row["snippet"],
                "publishedDate": row["published"],
            }
            for row in rows
        ],
    }


def _make_handler(state: FakeWebSearchState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(
            self,
            status: int,
            body: bytes = b"",
            content_type: str = "application/json",
            location: str | None = None,
        ) -> None:
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode())

        def _host(self) -> str:
            return self.headers.get("Host") or "fake-websearch:8080"

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                decoded = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                decoded = {}
            return decoded if isinstance(decoded, dict) else {}

        def _serve_page(self, path: str) -> None:
            with state.lock:
                state.page_fetches += 1
            if path == "/pages/jhin":
                self._send(200, _PAGE_JHIN.encode(), "text/html; charset=utf-8")
            elif path == "/pages/changelog":
                self._send(200, _PAGE_CHANGELOG.encode(), "text/html; charset=utf-8")
            elif path == "/pages/huge":
                self._send(200, _PAGE_HUGE.encode(), "text/html; charset=utf-8")
            elif path == "/pages/binary":
                self._send(200, _PNG_1X1, "image/png")
            elif path == "/pages/redirect":
                self._send(302, b"", "text/plain", location=f"http://{self._host()}/pages/jhin")
            elif path == "/pages/offsite":
                self._send(302, b"", "text/plain", location="https://example.com/")
            else:
                self._send_json(404, {"error": "no such page"})

        def do_GET(self) -> None:
            split = urlsplit(self.path)
            if split.path == "/_state":
                self._send_json(200, state.snapshot())
                return
            if split.path.startswith("/pages/"):
                self._serve_page(split.path)
                return
            if split.path == "/res/v1/web/search":
                if self.headers.get("X-Subscription-Token") != state.token:
                    self._send_json(401, {"error": "invalid subscription token"})
                    return
                query_params = parse_qs(split.query, keep_blank_values=True)
                query = query_params.get("q", [""])[0]
                try:
                    count = int(query_params.get("count", ["5"])[0])
                except ValueError:
                    count = 5
                state.record_search("brave", query)
                self._send_json(200, _brave_payload(_result_rows(self._host(), query, count)))
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            split = urlsplit(self.path)
            if split.path == "/_reset":
                state.reset()
                self._send_json(200, {"ok": True})
                return
            if split.path == "/search":
                body = self._read_json_body()
                query = str(body.get("query", ""))
                api_key = self.headers.get("x-api-key")
                if api_key is not None:
                    # Exa shape.
                    if api_key != state.token:
                        self._send_json(401, {"error": "invalid api key"})
                        return
                    limit = body.get("numResults")
                    count = limit if isinstance(limit, int) else 5
                    state.record_search("exa", query)
                    self._send_json(200, _exa_payload(_result_rows(self._host(), query, count)))
                    return
                # Tavily shape.
                if self.headers.get("Authorization") != f"Bearer {state.token}":
                    self._send_json(401, {"detail": "unauthorized"})
                    return
                limit = body.get("max_results")
                count = limit if isinstance(limit, int) else 5
                state.record_search("tavily", query)
                self._send_json(
                    200, _tavily_payload(_result_rows(self._host(), query, count), query)
                )
                return
            self._send_json(404, {"error": "not found"})

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


class FakeWebSearchServer:
    """Threaded fake web-search server usable as a pytest context manager."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, *, token: str = DEFAULT_TOKEN
    ) -> None:
        self.state = FakeWebSearchState(token=token)
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}"

    def __enter__(self) -> FakeWebSearchServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    port = int(os.environ.get("FAKE_WEBSEARCH_PORT", "8080"))
    state = FakeWebSearchState(token=os.environ.get("FAKE_WEBSEARCH_TOKEN", DEFAULT_TOKEN))
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    print(f"fake web search listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
