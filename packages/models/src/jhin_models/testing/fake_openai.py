"""A tiny fake OpenAI-compatible server (plan 32.2, 38).

Stdlib-only (``http.server``), so it runs anywhere jhin-models is installed:
as a pytest fixture, on a dev host, or as a compose service
(``python -m jhin_models.testing.fake_openai``). It implements just what the
adapters use: ``GET /v1/models`` (with OpenRouter-style pricing),
``POST /v1/chat/completions``, plus the two billing endpoints the balance
feature reads — ``GET /v1/credits`` (OpenRouter shape) and
``GET /v1/organization/costs`` (OpenAI Admin API shape) — with fixed numbers.

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

Phase 8 extensions for multi-agent scripts:

- ``[[b64:<base64>]]`` segments are decoded in place before marker
  extraction — exactly ONE layer per conversation scan. To make markers fire
  N conversation hops away, encode them N times: each hop's scan unwraps one
  layer, so a parent sees an inert ``[[b64:...]]`` string inside its own
  marker JSON while the child (whose task instructions carry the unwrapped
  blob) decodes it into real markers. Nested ``[[tool:...]]`` braces would
  otherwise break both the outer marker regex and JSON escaping.
- The literal token ``__VERDICT__`` inside a marker's arguments is replaced
  with ``pass``/``fail`` based on evidence: the most recent tool result that
  reports an ``exit_code`` (``0`` → pass). A QA script can therefore run the
  test suite and report an honest, evidence-based review verdict.
- Markers are also collected from **system** messages (before user messages,
  matching transcript order). Template-created review tasks compose their own
  instructions, so a scripted reviewer carries its behavior on the agent's
  system prompt instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FAIL_MODEL = "always-fails"
# Returns OpenAI's out-of-credit shape (HTTP 429, code "insufficient_quota").
NO_CREDIT_MODEL = "no-credit"
DEFAULT_MODELS = ("fake-mini", "fake-pro")

# Deterministic pricing advertised on ``GET /v1/models`` (OpenRouter shape:
# USD per token as strings) so the profile picker can auto-fill prices from a
# provider-supplied list, and context windows for the same rows.
FAKE_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "fake-mini": {"prompt": "0.00000015", "completion": "0.0000006", "context_length": 128000},
    "fake-pro": {"prompt": "0.0000025", "completion": "0.00001", "context_length": 200000},
}

# Deterministic billing figures (USD) for the two billing endpoints.
FAKE_TOTAL_CREDITS = 50.0
FAKE_TOTAL_USAGE = 12.5
FAKE_DAILY_COST_USD = 1.25
FAKE_COST_DAYS = 3


def build_credits() -> dict[str, Any]:
    """``GET /v1/credits`` — OpenRouter's shape."""
    return {"data": {"total_credits": FAKE_TOTAL_CREDITS, "total_usage": FAKE_TOTAL_USAGE}}


def build_organization_costs() -> dict[str, Any]:
    """``GET /v1/organization/costs`` — OpenAI Admin API shape, one page."""
    buckets = [
        {
            "object": "bucket",
            "start_time": 86_400 * day,
            "end_time": 86_400 * (day + 1),
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": FAKE_DAILY_COST_USD, "currency": "usd"},
                    "line_item": None,
                    "project_id": None,
                }
            ],
        }
        for day in range(FAKE_COST_DAYS)
    ]
    return {"object": "page", "data": buckets, "has_more": False, "next_page": None}


def build_models() -> dict[str, Any]:
    """``GET /v1/models`` with pricing/context for the default models."""
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "pricing": {
                    "prompt": FAKE_MODEL_PRICING[m]["prompt"],
                    "completion": FAKE_MODEL_PRICING[m]["completion"],
                },
                "context_length": FAKE_MODEL_PRICING[m]["context_length"],
            }
            for m in DEFAULT_MODELS
        ],
    }


# [[tool:<name> <flat-json-object>]] — payloads must not contain "]]".
TOOL_MARKER_RE = re.compile(r"\[\[tool:([a-z0-9_.]+)\s+(\{.*?\})\]\]", re.DOTALL)

# [[b64:<base64>]] — decoded into the message text before marker extraction.
B64_MARKER_RE = re.compile(r"\[\[b64:([A-Za-z0-9+/=]+)\]\]")

# Evidence placeholder substituted into marker arguments (see module docs).
VERDICT_TOKEN = "__VERDICT__"
_EXIT_CODE_RE = re.compile(r'"exit_code":\s*(-?\d+)')


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _expand_b64(text: str) -> str:
    """Decode ``[[b64:...]]`` segments — exactly ONE round per conversation
    scan. Single-round is load-bearing for multi-hop scripts: a blob that
    should fire two hops away is double-encoded, and each hop's scan unwraps
    exactly one layer (the parent sees an inert ``[[b64:...]]`` string inside
    its marker JSON; the child decodes it into real markers). Undecodable
    segments are left untouched."""

    def _decode(match: re.Match[str]) -> str:
        try:
            return base64.b64decode(match.group(1), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return match.group(0)

    return B64_MARKER_RE.sub(_decode, text)


def encode_marker_payload(text: str) -> str:
    """Helper for tests: wrap script text as a ``[[b64:...]]`` segment."""
    return f"[[b64:{base64.b64encode(text.encode('utf-8')).decode('ascii')}]]"


def _evidence_verdict(messages: list[dict[str, Any]]) -> str:
    """pass/fail from the most recent tool result carrying an exit_code;
    no evidence at all counts as a pass (nothing failed)."""
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        match = _EXIT_CODE_RE.search(str(message.get("content", "")))
        if match:
            return "pass" if match.group(1) == "0" else "fail"
    return "pass"


def _pending_tool_marker(messages: list[dict[str, Any]]) -> tuple[int, str, str] | None:
    """The next (index, tool_name, arguments_json) marker without a result.

    Markers are collected from user messages in order; each existing
    tool-role message consumes one marker. Returns None when every marker has
    been answered.
    """
    markers: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") in ("system", "user"):
            content = _expand_b64(str(message.get("content", "")))
            markers.extend(TOOL_MARKER_RE.findall(content))
    results = sum(1 for m in messages if m.get("role") == "tool")
    if results < len(markers):
        name, arguments = markers[results]
        if VERDICT_TOKEN in arguments:
            arguments = arguments.replace(VERDICT_TOKEN, _evidence_verdict(messages))
        return results, name, arguments
    return None


# --- deterministic dedup adjudication (jhin_memory.adjudication contract) ---

# Distinctive phrase of jhin_memory.adjudication.ADJUDICATION_SYSTEM_PROMPT.
ADJUDICATION_PROMPT_MARKER = "You compare pairs of remembered statements"
_ADJUDICATION_PAIR_RE = re.compile(r"^A: (.*)\nB: (.*)$", re.MULTILINE)
# Unlike the generic token regex, single digits ("retry limit is 3") count.
_ADJUDICATION_TOKEN_RE = re.compile(r"[a-z]{2,}|[0-9]+")
_WEEKDAYS = frozenset(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
)


def _adjudication_value_tokens(text: str) -> set[str]:
    """The "rare" value-bearing tokens of a statement: weekday names and
    tokens carrying digits."""
    return {
        token
        for token in _ADJUDICATION_TOKEN_RE.findall(text.casefold())
        if token in _WEEKDAYS or any(c.isdigit() for c in token)
    }


def _adjudication_reply(messages: list[dict[str, Any]]) -> str | None:
    """Deterministic strict-JSON verdicts for a dedup-adjudication request.

    Real models answer SAME/DIFFERENT per pair; the fake mirrors the contract
    so gray-zone dedup is exercisable offline: a pair is SAME when the two
    statements share at least one value token (weekday / number) and have no
    conflicting value tokens — i.e. their value-token sets are equal and
    non-empty ("every other Thursday" ↔ "every other Thursday" is SAME,
    "Thursday" vs "Friday" is DIFFERENT, and value-free pairs stay DIFFERENT).
    """
    system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
    if ADJUDICATION_PROMPT_MARKER not in system:
        return None
    user = next(
        (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), ""
    )
    verdicts: list[str] = []
    for a, b in _ADJUDICATION_PAIR_RE.findall(user):
        value_a = _adjudication_value_tokens(a)
        value_b = _adjudication_value_tokens(b)
        same = bool(value_a) and value_a == value_b
        verdicts.append("SAME" if same else "DIFFERENT")
    return json.dumps({"verdicts": verdicts})


# --- deterministic memory extraction (jhin_memory.extraction contract) ---

# Distinctive phrase of jhin_memory.extraction.EXTRACTION_SYSTEM_PROMPT.
MEMORY_PROMPT_MARKER = "You extract durable, reusable memory"
_TRANSCRIPT_RE = re.compile(r"<transcript>\n(.*)\n</transcript>", re.DOTALL)
_KNOWN_MEMORIES_RE = re.compile(r"<known_memories>\n(.*?)\n</known_memories>", re.DOTALL)
_REMEMBER_STRIP_RE = re.compile(r"(?i)^.*?\bremember\b(?:\s+that\b|:)?\s*")
_DELEGATION_LINE_RE = re.compile(r"^delegation from (.+?) \(")
_MEMORY_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _memory_tokens(text: str) -> set[str]:
    return set(_MEMORY_TOKEN_RE.findall(text.casefold()))


def _token_containment(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _memory_extraction_reply(messages: list[dict[str, Any]]) -> str | None:
    """Deterministic strict-JSON candidates for a memory-extraction request.

    Real models return the ``{"candidates": [...]}`` document demanded by
    ``jhin_memory.extraction``; the fake mirrors that so the memory feature is
    exercisable offline: every ``user:`` transcript line containing the word
    "remember" becomes one fact candidate (the text after "remember that"),
    and a delegated task's exchange yields one fact about who delegated what.
    Candidates overlapping an entry of the known-memories block (or an earlier
    candidate) at ≥ 0.6 token containment are skipped — the deterministic
    analogue of the prompt's "only NEW or CHANGED facts" rule.
    """
    system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
    if MEMORY_PROMPT_MARKER not in system:
        return None
    user = next(
        (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), ""
    )
    transcript_match = _TRANSCRIPT_RE.search(user)
    transcript = transcript_match.group(1) if transcript_match else ""
    known_match = _KNOWN_MEMORIES_RE.search(user)
    known_tokens = [
        _memory_tokens(line[2:])
        for line in (known_match.group(1).splitlines() if known_match else [])
        if line.startswith("- ") and line[2:].strip()
    ]

    proposals: list[str] = []
    task_title = ""
    for raw_line in transcript.splitlines():
        line = raw_line.strip()
        if line.startswith("task: "):
            task_title = line[len("task: ") :]
        delegated = _DELEGATION_LINE_RE.match(line)
        if delegated and task_title:
            proposals.append(f"{delegated.group(1).strip()} delegated: {task_title}"[:280])
        if line.startswith("user: ") and re.search(r"(?i)\bremember\b", line):
            fact = _REMEMBER_STRIP_RE.sub("", line[len("user: ") :]).strip().rstrip(".")
            if fact:
                proposals.append(fact[:280])

    candidates: list[dict[str, Any]] = []
    accepted_tokens: list[set[str]] = []
    for proposal in proposals:
        tokens = _memory_tokens(proposal)
        if not tokens:
            continue
        if any(_token_containment(tokens, entry) >= 0.6 for entry in known_tokens):
            continue
        if any(_token_containment(tokens, entry) >= 0.6 for entry in accepted_tokens):
            continue
        accepted_tokens.append(tokens)
        candidates.append(
            {
                "content": proposal,
                "kind": "fact",
                "confidence": 0.75,
                "importance": 0.6,
                "requested_scope": "agent",
            }
        )
        if len(candidates) >= 5:
            break
    return json.dumps({"candidates": candidates})


def build_completion(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Pure request→response logic, separated for direct unit testing."""
    model = str(body.get("model", "fake-mini"))
    if model == FAIL_MODEL:
        error = {"message": "fake provider: simulated failure", "type": "server_error"}
        return 500, {"error": error}
    if model == NO_CREDIT_MODEL:
        return 429, {
            "error": {
                "message": "You exceeded your current quota, please check your plan and billing",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        }

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

    for structured_reply in (_adjudication_reply(messages), _memory_extraction_reply(messages)):
        if structured_reply is None:
            continue
        usage["completion_tokens"] = _estimate_tokens(structured_reply)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        envelope["choices"] = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": structured_reply},
                "finish_reason": "stop",
            }
        ]
        return 200, envelope

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


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def deterministic_png(prompt: str, *, side: int = 64) -> bytes:
    """A tiny solid-colour PNG whose colour is derived from ``prompt``.

    Stdlib only (no Pillow) so the fake provider stays dependency-free; the
    result decodes as a real PNG so downstream image pipelines can be tested
    end to end.
    """
    digest = hashlib.sha256(prompt.encode()).digest()
    red, green, blue = digest[0], digest[1], digest[2]
    row = b"\x00" + bytes((red, green, blue)) * side
    raw = row * side
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def build_image_generation(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST /v1/images/generations``: deterministic inline PNG.

    ``model == "always-fails"`` returns HTTP 500 like the chat endpoint.
    """
    model = str(body.get("model", "fake-image-model"))
    if model == "always-fails":
        return 500, {"error": {"message": "simulated provider failure"}}
    prompt = str(body.get("prompt", ""))
    png = deterministic_png(prompt)
    return 200, {
        "created": 0,
        "model": model,
        "data": [{"b64_json": base64.b64encode(png).decode()}],
    }


DEFAULT_EMBEDDING_DIMENSIONS = 64
_EMBED_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def deterministic_embedding(
    text: str, *, dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS
) -> list[float]:
    """Hashed bag-of-words vector of fixed ``dimensions``, L2-normalised.

    Each token is hashed (sha256) to one coordinate and a sign, so texts that
    share words are close in cosine space and unrelated texts are near
    orthogonal. Deterministic across processes (no ``hash()`` randomisation),
    which makes semantic-ranking tests meaningful offline.
    """
    dimensions = max(1, dimensions)
    vector = [0.0] * dimensions
    tokens = _EMBED_TOKEN_RE.findall(text.casefold())
    if not tokens:
        vector[0] = 1.0
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = sum(v * v for v in vector) ** 0.5
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


def build_embeddings(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """``POST /v1/embeddings``: deterministic pseudo-embeddings.

    ``model == "always-fails"`` returns HTTP 500 like the chat endpoint;
    ``dimensions`` is honoured (default 64). Usage is length-derived.
    """
    model = str(body.get("model", "fake-embed"))
    if model == FAIL_MODEL:
        return 500, {"error": {"message": "simulated provider failure"}}
    raw_input = body.get("input", [])
    texts = [raw_input] if isinstance(raw_input, str) else [str(t) for t in raw_input]
    dimensions = int(body.get("dimensions") or DEFAULT_EMBEDDING_DIMENSIONS)
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": deterministic_embedding(t, dimensions=dimensions),
        }
        for i, t in enumerate(texts)
    ]
    prompt_tokens = sum(_estimate_tokens(t) for t in texts)
    return 200, {
        "object": "list",
        "model": model,
        "data": data,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
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
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/models"):
            self._send_json(200, build_models())
        elif path.endswith("/credits"):
            self._send_json(200, build_credits())
        elif path.endswith("/organization/costs"):
            self._send_json(200, build_organization_costs())
        else:
            self._send_json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        routes = ("/chat/completions", "/images/generations", "/embeddings")
        if not path.endswith(routes):
            self._send_json(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return
        if path.endswith("/images/generations"):
            status, payload = build_image_generation(body)
        elif path.endswith("/embeddings"):
            status, payload = build_embeddings(body)
        else:
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
