"""A tiny fake OpenAI-compatible server (plan 32.2, 38).

Stdlib-only (``http.server``), so it runs anywhere jhin-models is installed:
as a pytest fixture, on a dev host, or as a compose service
(``python -m jhin_models.testing.fake_openai``). It implements just what the
adapters use: ``GET /v1/models`` and ``POST /v1/chat/completions``.

Deterministic behavior tests rely on:

- The completion text is ``"[<model>] " + reply``, where the reply echoes the
  last user message, so assertions can tie a response to a model name.
- Usage is length-derived (chars // 4), so token counts are stable.
- ``model == "always-fails"`` returns HTTP 500 (error-path testing).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FAIL_MODEL = "always-fails"
DEFAULT_MODELS = ("fake-mini", "fake-pro")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_completion(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Pure request→response logic, separated for direct unit testing."""
    model = str(body.get("model", "fake-mini"))
    if model == FAIL_MODEL:
        error = {"message": "fake provider: simulated failure", "type": "server_error"}
        return 500, {"error": error}

    messages = body.get("messages", [])
    last_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user = str(message.get("content", ""))
            break
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)

    reply = f"[{model}] Completed: {last_user[:200].strip() or 'no instruction given'}"
    return 200, {
        "id": f"fakecmpl-{abs(hash((model, prompt_chars))) % 10**10}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": _estimate_tokens("x" * prompt_chars),
            "completion_tokens": _estimate_tokens(reply),
            "total_tokens": _estimate_tokens("x" * prompt_chars) + _estimate_tokens(reply),
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(
                200,
                {"object": "list", "data": [{"id": m, "object": "model"} for m in DEFAULT_MODELS]},
            )
        else:
            self._send_json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return
        status, payload = build_completion(body)
        self._send_json(status, payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep pytest output clean


class FakeOpenAIServer:
    """Threaded fake provider; use as a context manager in tests."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._server.server_address[1]}/v1"

    def __enter__(self) -> FakeOpenAIServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def main() -> None:
    import os

    port = int(os.environ.get("FAKE_PROVIDER_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"fake OpenAI-compatible provider listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
