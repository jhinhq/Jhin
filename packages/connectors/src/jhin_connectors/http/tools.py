"""Tool definitions + executors for the generic HTTP connector.

Two tools, split by risk because approval policies act on a tool's risk:

- ``http.get`` (read) — safe methods GET/HEAD;
- ``http.request`` (write, approvable) — POST/PUT/PATCH/DELETE.

Both scope on ``connection_id``, ``method``, and ``path``: grants can pin a
connection, restrict methods, and glob paths (``/v1/*``) with the standard
pattern matching. Responses are bounded, text-only, and labeled untrusted.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from pydantic import BaseModel

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.execution import resolve_connection
from jhin_connectors.http.client import (
    auth_headers,
    default_headers_from_config,
    http_client,
    join_url,
    request_headers,
    send_bounded_text,
    validate_http_base_url,
)
from jhin_connectors.http.manifest import HTTP_CONNECTOR_TYPE
from jhin_connectors.http.schemas import HttpGetInput, HttpRequestInput, HttpResponseOutput
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.errors import ToolExecutionError

_SCOPE_KEYS: tuple[str, ...] = ("connection_id", "method", "path")
_INVALID_REQUEST_HINT = (
    "Use a relative path (no scheme, host, or '..'), put query parameters in "
    "`query`, and never set authentication or cookie headers directly."
)


async def _execute(
    ctx: ToolExecutionContext,
    data: HttpGetInput | HttpRequestInput,
    *,
    json_body: Any = None,
    safe_method: bool,
) -> HttpResponseOutput:
    resolved = await resolve_connection(ctx, data.connection_id, connector_type=HTTP_CONNECTOR_TYPE)
    try:
        base_url = validate_http_base_url(str(resolved.config.get("base_url") or ""))
        headers = default_headers_from_config(resolved.config)
        headers.update(request_headers(data.headers))
        headers.update(
            auth_headers(resolved.connection.auth_type, resolved.credentials, resolved.config)
        )
        url = join_url(base_url, data.path)
    except (EndpointPolicyError, ValueError) as error:
        raise ToolExecutionError(
            str(error),
            code="http_invalid_request",
            side_effect_possible=False,
            hint=_INVALID_REQUEST_HINT,
        ) from None
    try:
        async with http_client(headers) as client:
            request = client.build_request(
                data.method, url, params=data.query or None, json=json_body
            )
            status, content_type, text, truncated = await send_bounded_text(client, request)
    except httpx.HTTPError as error:
        # Safe methods provably cause no effect; for mutations a transport
        # failure after the request may have reached the server fails closed.
        raise ToolExecutionError(
            f"the HTTP request failed before a response was read ({type(error).__name__})",
            code="http_request_failed",
            side_effect_possible=not safe_method,
        ) from None
    return HttpResponseOutput(
        status_code=status,
        content_type=content_type,
        text=text,
        truncated=truncated,
        is_error=status >= 400,
    )


async def _get(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(HttpGetInput, payload)
    return await _execute(ctx, data, safe_method=True)


async def _request(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(HttpRequestInput, payload)
    return await _execute(ctx, data, json_body=data.json_body, safe_method=False)


HTTP_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="http.get",
            description=(
                "Read (GET/HEAD) a path on the connection's base URL. The bounded "
                "text/JSON response body is untrusted external content."
            ),
            risk=RiskLevel.READ,
            input_model=HttpGetInput,
            output_model=HttpResponseOutput,
            required_capability="http.get",
            scope_keys=_SCOPE_KEYS,
        ),
        _get,
    ),
    (
        ToolDefinition(
            name="http.request",
            description=(
                "Send a mutating request (POST/PUT/PATCH/DELETE) to a path on the "
                "connection's base URL, optionally with a JSON body."
            ),
            risk=RiskLevel.WRITE,
            input_model=HttpRequestInput,
            output_model=HttpResponseOutput,
            required_capability="http.request",
            supports_approval=True,
            scope_keys=_SCOPE_KEYS,
        ),
        _request,
    ),
)
