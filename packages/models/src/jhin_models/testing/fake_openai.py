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

Tool calling (Phase 4): a user message may embed markers of the form::

    [[tool:system.echo {"text": "hi"}]]

For each marker that does not yet have a tool-role result message in the
conversation, the fake responds with one OpenAI-style ``tool_calls`` entry
(one per response, in marker order). Once every marker has a result, it
responds with a normal completion summarizing the tool outputs.

The fake deliberately emits tool calls even when the request advertises no
``tools`` — a real model can hallucinate tool names it was never offered, and
authorization must live in the tool gateway, not in the prompt (plan 52).
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FAIL_MODEL = "always-fails"
DEFAULT_MODELS = ("fake-mini", "fake-pro")

# [[tool:<name> <flat-json-object>]] — payloads must not contain "]]".
TOOL_MARKER_RE = re.compile(r"\[\[tool:([a-z0-9_.]+)\s+(\{.*?\})\]\]", re.DOTALL)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _pending_tool_marker(messages: list[dict[str, Any]]) -> tuple[int, str, str] | None:
    """The next (index, tool_name, arguments_json) marker without a result.

    Markers are collected from user messages in order; each existing
    tool-role message consumes one marker. Returns None when every marker has
    been answered.
    """
    markers: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") == "user":
            markers.extend(TOOL_MARKER_RE.findall(str(message.get("content", ""))))
    results = sum(1 for m in messages if m.get("role") == "tool")
    if results < len(markers):
        name, arguments = markers[results]
        return results, name, arguments
    return None


def build_completion(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Pure request→response logic, separated for direct unit testing."""
    model = str(body.get("model", "fake-mini"))
    if model == FAIL_MODEL:
        error = {"message": "fake provider: simulated failure", "type": "server_error"}
        return 500, {"error": error}

    messages = body.get("messages", [])
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    usage = {
        "prompt_tokens": _estimate_tokens("x" * prompt_chars),
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    envelope: dict[str, Any] = {
        "id": f"fakecmpl-{abs(hash((model, prompt_chars, len(messages)))) % 10**10}",
        "object": "chat.completion",
        "model": model,
        "usage": usage,
    }

    pending = _pending_tool_marker(messages)
    if pending is not None:
        index, tool_name, arguments_json = pending
        usage["completion_tokens"] = _estimate_tokens(arguments_json)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        envelope["choices"] = [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments_json},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
        return 200, envelope

    tool_results = [str(m.get("content", "")) for m in messages if m.get("role") == "tool"]
    if tool_results:
        reply = f"[{model}] Done after {len(tool_results)} tool call(s). Last result: " + (
            tool_results[-1][:200].strip()
        )
    else:
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        reply = f"[{model}] Completed: {last_user[:200].strip() or 'no instruction given'}"

    usage["completion_tokens"] = _estimate_tokens(reply)
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    envelope["choices"] = [
        {
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }
    ]
    return 200, envelope


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
