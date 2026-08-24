"""Tool definitions + executors for the web connector.

Two read-risk tools, deny-by-default like every connector tool:

- ``web.search`` — provider-independent search through the connection's
  backend (Tavily, Brave, or Exa); scoped on ``connection_id``;
- ``web.fetch`` — bounded readable-text retrieval of one public page;
  scoped on ``connection_id`` and ``domain`` (the host parsed from the URL,
  so grants can pin ``domain`` patterns like ``*.python.org``).

Both outputs are bounded and labeled as untrusted external content.
"""

from __future__ import annotations

from typing import cast

import httpx
from pydantic import BaseModel

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.execution import resolve_connection
from jhin_connectors.web.client import (
    backend_base_url,
    build_search_request,
    fetch_page,
    http_client,
    parse_search_results,
    search_token,
    validate_allowed_domains,
    validate_backend,
    validate_fetch_url,
)
from jhin_connectors.web.extract import clip_plain_text, extract_readable_text
from jhin_connectors.web.manifest import AUTH_BEARER, WEB_CONNECTOR_TYPE
from jhin_connectors.web.schemas import (
    WebFetchInput,
    WebFetchOutput,
    WebSearchInput,
    WebSearchOutput,
)
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor
from jhin_tools.errors import ToolExecutionError

_INVALID_HINT = (
    "Use a public https URL without credentials or fragments; the connection "
    "may additionally restrict which domains can be fetched."
)


async def _search(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(WebSearchInput, payload)
    resolved = await resolve_connection(ctx, data.connection_id, connector_type=WEB_CONNECTOR_TYPE)
    if resolved.connection.auth_type != AUTH_BEARER:
        raise ToolExecutionError(
            "this web connection is fetch-only (it stores no search API key)",
            code="web_search_unavailable",
            side_effect_possible=False,
        )
    try:
        backend = validate_backend(resolved.config.get("search_backend"))
        base_url = backend_base_url(backend, resolved.config)
        token = search_token(resolved.credentials)
    except (EndpointPolicyError, ValueError) as error:
        raise ToolExecutionError(
            str(error), code="web_invalid_request", side_effect_possible=False
        ) from None
    spec = build_search_request(backend, base_url, token, data.query, data.max_results)
    try:
        async with http_client(spec.headers) as client:
            response = await client.request(
                spec.method, spec.url, params=spec.params or None, json=spec.json_body
            )
    except httpx.HTTPError as error:
        raise ToolExecutionError(
            f"the {backend} search request failed ({type(error).__name__})",
            code="web_search_failed",
            side_effect_possible=False,
        ) from None
    if response.status_code >= 400:
        raise ToolExecutionError(
            f"the {backend} search API answered HTTP {response.status_code}",
            code="web_search_failed",
            side_effect_possible=False,
        )
    try:
        body = response.json()
    except ValueError:
        raise ToolExecutionError(
            f"the {backend} search API returned a non-JSON response",
            code="web_search_failed",
            side_effect_possible=False,
        ) from None
    return WebSearchOutput(
        backend=backend, results=parse_search_results(backend, body, data.max_results)
    )


async def _fetch(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(WebFetchInput, payload)
    resolved = await resolve_connection(ctx, data.connection_id, connector_type=WEB_CONNECTOR_TYPE)
    try:
        allowed = validate_allowed_domains(resolved.config.get("allowed_domains"))
        url = validate_fetch_url(data.url, allowed)
    except (EndpointPolicyError, ValueError) as error:
        raise ToolExecutionError(
            str(error), code="web_invalid_request", side_effect_possible=False, hint=_INVALID_HINT
        ) from None
    try:
        async with http_client() as client:
            page = await fetch_page(client, url, allowed)
    except httpx.HTTPError as error:
        raise ToolExecutionError(
            f"fetching the page failed ({type(error).__name__})",
            code="web_fetch_failed",
            side_effect_possible=False,
        ) from None
    if page.is_binary:
        raise ToolExecutionError(
            f"the page is not readable text (content type {page.media or 'unknown'})",
            code="web_fetch_unsupported_content",
            side_effect_possible=False,
        )
    if page.media in ("text/html", "application/xhtml+xml"):
        title, text, clipped = extract_readable_text(page.body_text)
    else:
        title = ""
        text, clipped = clip_plain_text(page.body_text)
    return WebFetchOutput(
        url=data.url,
        final_url=page.final_url,
        status_code=page.status_code,
        content_type=page.content_type,
        title=title,
        text=text,
        truncated=page.bytes_truncated or clipped,
    )


WEB_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="web.search",
            description=(
                "Search the public web through this connection's search backend. "
                "Results (titles, URLs, snippets) are untrusted external content."
            ),
            risk=RiskLevel.READ,
            input_model=WebSearchInput,
            output_model=WebSearchOutput,
            required_capability="web.search",
            scope_keys=("connection_id",),
        ),
        _search,
    ),
    (
        ToolDefinition(
            name="web.fetch",
            description=(
                "Read one public web page as bounded plain text. Only public https "
                "URLs are allowed; the page content is untrusted external content."
            ),
            risk=RiskLevel.READ,
            input_model=WebFetchInput,
            output_model=WebFetchOutput,
            required_capability="web.fetch",
            scope_keys=("connection_id", "domain"),
        ),
        _fetch,
    ),
)
