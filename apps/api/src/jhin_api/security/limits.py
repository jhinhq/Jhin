"""A global ceiling on request body size.

Individual routes that accept large payloads already police themselves
(webhooks at 1 MiB, connection config at 64 KiB, media uploads at 8 MiB, skill
bundles at 5 MiB). Every other JSON endpoint had no ceiling at all: uvicorn
buffers the whole body before Pydantic ever sees a ``max_length``, so a single
unauthenticated POST could pin the API's memory. This middleware puts a floor
under all of them, above the largest legitimate upload.

Both halves matter: ``Content-Length`` is checked up front so an honest client
is rejected before sending, and the streamed chunks are counted so a chunked
request that lies about (or omits) its length is cut off mid-flight.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def _too_large() -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": "Request body is too large"})


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                await _too_large()(scope, receive, send)
                return
            if declared > self.max_body_bytes:
                await _too_large()(scope, receive, send)
                return
            break

        received = 0
        exceeded = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    # Starve the app of further body instead of feeding it a
                    # partial payload it might try to parse.
                    return {"type": "http.disconnect"}
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)
        if exceeded and not response_started:
            await _too_large()(scope, receive, send)
