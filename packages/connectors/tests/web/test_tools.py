"""Executor behavior (backend shapes, extraction bounds, redirects, policy)
and gateway grant/scope matching for the web connector tools."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

import jhin_connectors.web.tools as web_tools
from jhin_connectors.registry import build_default_catalog
from jhin_connectors.web.connector import WebConnector
from jhin_connectors.web.schemas import (
    UNTRUSTED_NOTICE,
    WebFetchInput,
    WebFetchOutput,
    WebSearchInput,
    WebSearchOutput,
)
from jhin_policy import Grant, GrantEffect, evaluate
from jhin_tools.errors import ToolExecutionError

EXECUTORS = {definition.name: executor for definition, executor in WebConnector().tools()}
BASE = "http://fake-websearch:8080"


@pytest.fixture
def allow_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", BASE)


def _mock_http_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def factory(headers=None):  # type: ignore[no-untyped-def]
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            headers=dict(headers or {}),
        )

    monkeypatch.setattr(web_tools, "http_client", factory)


def _connection_factory(make_connection, workspace):  # type: ignore[no-untyped-def]
    async def factory(backend: str = "tavily", **overrides):  # type: ignore[no-untyped-def]
        config = {"search_backend": backend, "base_url": BASE}
        config.update(overrides.pop("config", {}))
        return await make_connection(
            workspace,
            connector_type="web",
            name=f"Web {backend} {overrides.pop('name_suffix', '')}",
            auth_type=overrides.pop("auth_type", "bearer"),
            credentials=overrides.pop("credentials", {"token": "web-secret-token"}),
            config=config,
        )

    return factory


@pytest.fixture
def make_web_connection(workspace, make_connection, allow_fake):  # type: ignore[no-untyped-def]
    return _connection_factory(make_connection, workspace)


# --- web.search ----------------------------------------------------------------


async def test_tavily_search_sends_bearer_and_normalizes(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "About Jhin",
                        "url": f"{BASE}/pages/jhin",
                        "content": "platform overview",
                        "published_date": "2026-08-01",
                    }
                ]
            },
        )

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection("tavily")
    output = await EXECUTORS["web.search"](
        context,
        WebSearchInput(connection_id=str(connection.id), query="jhin platform", max_results=2),
    )
    assert isinstance(output, WebSearchOutput)
    assert output.backend == "tavily"
    assert output.notice == UNTRUSTED_NOTICE
    assert [result.title for result in output.results] == ["About Jhin"]
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/search"
    assert request.headers["Authorization"] == "Bearer web-secret-token"
    assert json.loads(request.content) == {"query": "jhin platform", "max_results": 2}


async def test_brave_search_uses_subscription_token_and_params(
    monkeypatch, context, make_web_connection
):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [{"title": "B", "url": "https://b.example/", "description": "d"}]
                }
            },
        )

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection("brave", name_suffix="b")
    output = await EXECUTORS["web.search"](
        context, WebSearchInput(connection_id=str(connection.id), query="jhin", max_results=3)
    )
    assert isinstance(output, WebSearchOutput)
    assert output.results[0].url == "https://b.example/"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/res/v1/web/search"
    assert request.url.params["q"] == "jhin"
    assert request.url.params["count"] == "3"
    assert request.headers["X-Subscription-Token"] == "web-secret-token"


async def test_exa_search_uses_api_key_header(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"results": [{"title": "E", "url": "https://e.example/", "text": "t"}]}
        )

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection("exa", name_suffix="e")
    output = await EXECUTORS["web.search"](
        context, WebSearchInput(connection_id=str(connection.id), query="jhin")
    )
    assert isinstance(output, WebSearchOutput)
    assert output.results[0].snippet == "t"
    request = seen[0]
    assert request.headers["x-api-key"] == "web-secret-token"
    assert json.loads(request.content) == {"query": "jhin", "numResults": 5}


async def test_search_error_statuses_fail_cleanly(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(monkeypatch, lambda request: httpx.Response(429, json={"error": "slow"}))
    connection = await make_web_connection("tavily", name_suffix="429")
    with pytest.raises(ToolExecutionError) as info:
        await EXECUTORS["web.search"](
            context, WebSearchInput(connection_id=str(connection.id), query="jhin")
        )
    assert info.value.code == "web_search_failed"
    assert info.value.side_effect_possible is False


async def test_fetch_only_connection_cannot_search(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("fetch-only connections must never call a search API")

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection(
        auth_type="none",
        credentials={},
        config={"search_backend": "", "base_url": ""},
        name_suffix="fetchonly",
    )
    with pytest.raises(ToolExecutionError) as info:
        await EXECUTORS["web.search"](
            context, WebSearchInput(connection_id=str(connection.id), query="jhin")
        )
    assert info.value.code == "web_search_unavailable"


# --- web.fetch -----------------------------------------------------------------


async def test_fetch_extracts_readable_text(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    html = (
        "<html><head><title>About Jhin</title><script>evil()</script></head>"
        "<body><p>Jhin agents get internet access.</p></body></html>"
    )
    _mock_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"}
        ),
    )
    connection = await make_web_connection("tavily", name_suffix="fetch")
    output = await EXECUTORS["web.fetch"](
        context, WebFetchInput(connection_id=str(connection.id), url=f"{BASE}/pages/jhin")
    )
    assert isinstance(output, WebFetchOutput)
    assert output.title == "About Jhin"
    assert "internet access" in output.text
    assert "evil" not in output.text
    assert output.status_code == 200
    assert output.notice == UNTRUSTED_NOTICE


async def test_fetch_rejects_binary_bodies(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"\x89PNG...", headers={"content-type": "image/png"}
        ),
    )
    connection = await make_web_connection("tavily", name_suffix="bin")
    with pytest.raises(ToolExecutionError) as info:
        await EXECUTORS["web.fetch"](
            context, WebFetchInput(connection_id=str(connection.id), url=f"{BASE}/pages/binary")
        )
    assert info.value.code == "web_fetch_unsupported_content"


async def test_fetch_truncates_huge_pages(monkeypatch, context, make_web_connection):  # type: ignore[no-untyped-def]
    huge = "<p>" + ("padding sentence " * 20_000) + "</p>"
    _mock_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=huge.encode(), headers={"content-type": "text/html"}
        ),
    )
    connection = await make_web_connection("tavily", name_suffix="huge")
    output = await EXECUTORS["web.fetch"](
        context, WebFetchInput(connection_id=str(connection.id), url=f"{BASE}/pages/huge")
    )
    assert isinstance(output, WebFetchOutput)
    assert output.truncated is True
    assert len(output.text) <= 20_000


async def test_fetch_follows_same_origin_but_never_cross_origin_redirects(
    monkeypatch, context, make_web_connection
):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "fake-websearch":
            raise AssertionError("cross-origin redirect target must never be fetched")
        if request.url.path == "/pages/redirect":
            return httpx.Response(302, headers={"location": "/pages/jhin"})
        if request.url.path == "/pages/offsite":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        return httpx.Response(200, content=b"<p>landed</p>", headers={"content-type": "text/html"})

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection("tavily", name_suffix="redir")

    followed = await EXECUTORS["web.fetch"](
        context, WebFetchInput(connection_id=str(connection.id), url=f"{BASE}/pages/redirect")
    )
    assert isinstance(followed, WebFetchOutput)
    assert followed.status_code == 200
    assert "landed" in followed.text
    assert followed.final_url.endswith("/pages/jhin")

    stopped = await EXECUTORS["web.fetch"](
        context, WebFetchInput(connection_id=str(connection.id), url=f"{BASE}/pages/offsite")
    )
    assert isinstance(stopped, WebFetchOutput)
    assert stopped.status_code == 302
    assert stopped.text == ""


async def test_fetch_rejects_private_urls_without_a_request(
    monkeypatch, context, make_web_connection
):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a disallowed URL")

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection("tavily", name_suffix="priv")
    with pytest.raises(ToolExecutionError) as info:
        await EXECUTORS["web.fetch"](
            context, WebFetchInput(connection_id=str(connection.id), url="http://internal:9/x")
        )
    assert info.value.code == "web_invalid_request"
    assert info.value.side_effect_possible is False


async def test_fetch_enforces_the_connection_allowed_domains(
    monkeypatch, context, make_web_connection
):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("out-of-domain URL must never be fetched")

    _mock_http_client(monkeypatch, handler)
    connection = await make_web_connection(
        "tavily", name_suffix="dom", config={"allowed_domains": ["docs.example.com"]}
    )
    with pytest.raises(ToolExecutionError) as info:
        await EXECUTORS["web.fetch"](
            context,
            WebFetchInput(connection_id=str(connection.id), url="https://other.example.com/page"),
        )
    assert info.value.code == "web_invalid_request"


# --- gateway grant/scope matching ---------------------------------------------

CONNECTION = "0198c5f2-0000-7000-8000-00000000000a"


def _definition(name: str):  # type: ignore[no-untyped-def]
    definition = build_default_catalog().registry.get(name)
    assert definition is not None
    return definition


def test_domain_glob_grants_match_requests() -> None:
    decision = evaluate(
        _definition("web.fetch"),
        grants=[
            Grant(
                capability="web.fetch",
                scope={"connection_id": CONNECTION, "domain": "*.python.org"},
                effect=GrantEffect.ALLOW,
            )
        ],
        rules=[],
        requested_scope={"connection_id": CONNECTION, "domain": "docs.python.org"},
    )
    assert decision.decision.value == "allow"


def test_domain_outside_grant_is_denied() -> None:
    decision = evaluate(
        _definition("web.fetch"),
        grants=[
            Grant(
                capability="web.fetch",
                scope={"connection_id": CONNECTION, "domain": "*.python.org"},
                effect=GrantEffect.ALLOW,
            )
        ],
        rules=[],
        requested_scope={"connection_id": CONNECTION, "domain": "evil.example"},
    )
    assert decision.decision.value == "deny"
    assert decision.code == "scope_mismatch"


def test_search_without_grant_is_denied() -> None:
    decision = evaluate(
        _definition("web.search"),
        grants=[],
        rules=[],
        requested_scope={"connection_id": CONNECTION},
    )
    assert decision.decision.value == "deny"
    assert decision.code == "no_grant"
