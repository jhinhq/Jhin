"""Executor behavior (bounded output, redirects, failures) and gateway
grant/scope matching for the generic HTTP tools."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

import jhin_connectors.http.tools as http_tools
from jhin_connectors.http.connector import HttpConnector
from jhin_connectors.http.schemas import HttpGetInput, HttpRequestInput, HttpResponseOutput
from jhin_connectors.registry import build_default_catalog
from jhin_policy import Grant, GrantEffect, evaluate
from jhin_tools.errors import ToolExecutionError

EXECUTORS = {definition.name: executor for definition, executor in HttpConnector().tools()}
BASE = "http://fake-api:8080"


@pytest.fixture
def allow_fake_api(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(http_tools, "http_client", factory)


@pytest.fixture
async def http_connection(workspace, make_connection, allow_fake_api):  # type: ignore[no-untyped-def]
    return await make_connection(
        workspace,
        connector_type="http",
        auth_type="bearer",
        credentials={"token": "http-secret-token"},
        config={"base_url": BASE, "default_headers": ["X-Env: test"]},
    )


async def test_get_sends_auth_and_default_headers_and_bounds_json(
    monkeypatch, context, http_connection
):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    _mock_http_client(monkeypatch, handler)
    output = await EXECUTORS["http.get"](
        context,
        HttpGetInput(connection_id=str(http_connection.id), path="/v1/items", query={"page": "2"}),
    )

    assert isinstance(output, HttpResponseOutput)
    assert output.status_code == 200
    assert json.loads(output.text) == {"ok": True}
    assert output.is_error is False
    assert output.truncated is False
    assert "Untrusted output" in output.notice
    request = seen[0]
    assert str(request.url) == f"{BASE}/v1/items?page=2"
    assert request.headers["Authorization"] == "Bearer http-secret-token"
    assert request.headers["X-Env"] == "test"


async def test_request_posts_json_body(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 7})

    _mock_http_client(monkeypatch, handler)
    output = await EXECUTORS["http.request"](
        context,
        HttpRequestInput(
            connection_id=str(http_connection.id),
            method="POST",
            path="/v1/items",
            json_body={"name": "widget"},
        ),
    )

    assert isinstance(output, HttpResponseOutput)
    assert output.status_code == 201
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"name": "widget"}


async def test_error_statuses_are_reported_not_raised(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(monkeypatch, lambda request: httpx.Response(404, json={"detail": "nope"}))
    output = await EXECUTORS["http.get"](
        context, HttpGetInput(connection_id=str(http_connection.id), path="/missing")
    )
    assert isinstance(output, HttpResponseOutput)
    assert output.status_code == 404
    assert output.is_error is True


async def test_redirects_are_never_followed(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        raise AssertionError("redirect target must never be fetched")

    _mock_http_client(monkeypatch, handler)
    output = await EXECUTORS["http.get"](
        context, HttpGetInput(connection_id=str(http_connection.id), path="/start")
    )
    assert isinstance(output, HttpResponseOutput)
    assert output.status_code == 302


async def test_huge_bodies_are_truncated(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(
        monkeypatch,
        lambda request: httpx.Response(200, text="x" * 100_000),
    )
    output = await EXECUTORS["http.get"](
        context, HttpGetInput(connection_id=str(http_connection.id))
    )
    assert isinstance(output, HttpResponseOutput)
    assert output.truncated is True
    assert len(output.text) <= 20_000 + len("…[truncated]")


async def test_binary_bodies_become_a_placeholder(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"\x89PNG...", headers={"content-type": "image/png"}
        ),
    )
    output = await EXECUTORS["http.get"](
        context, HttpGetInput(connection_id=str(http_connection.id))
    )
    assert isinstance(output, HttpResponseOutput)
    assert output.text == "[response body omitted: unsupported content type image/png]"


async def test_traversal_paths_fail_without_side_effect(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for an invalid path")

    _mock_http_client(monkeypatch, handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        await EXECUTORS["http.get"](
            context,
            HttpGetInput(connection_id=str(http_connection.id), path="/v1/../secrets"),
        )
    assert exc_info.value.code == "http_invalid_request"
    assert exc_info.value.side_effect_possible is False


async def test_caller_auth_headers_are_rejected(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    _mock_http_client(monkeypatch, lambda request: httpx.Response(200))
    with pytest.raises(ToolExecutionError) as exc_info:
        await EXECUTORS["http.get"](
            context,
            HttpGetInput(
                connection_id=str(http_connection.id),
                headers={"Authorization": "Bearer sneaky"},
            ),
        )
    assert exc_info.value.code == "http_invalid_request"


async def test_transport_failure_on_write_fails_closed(monkeypatch, context, http_connection):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _mock_http_client(monkeypatch, handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        await EXECUTORS["http.request"](
            context,
            HttpRequestInput(connection_id=str(http_connection.id), method="DELETE", path="/v1/x"),
        )
    assert exc_info.value.side_effect_possible is True

    with pytest.raises(ToolExecutionError) as get_info:
        await EXECUTORS["http.get"](context, HttpGetInput(connection_id=str(http_connection.id)))
    assert get_info.value.side_effect_possible is False


# --- gateway grant/scope matching ---

CONNECTION = "0198c5f2-0000-7000-8000-000000000002"


def _definition(name: str):  # type: ignore[no-untyped-def]
    definition = build_default_catalog().registry.get(name)
    assert definition is not None
    return definition


def _decision(name: str, grant_scope: dict[str, str], requested: dict[str, str]):  # type: ignore[no-untyped-def]
    return evaluate(
        _definition(name),
        grants=[Grant(capability=name, scope=grant_scope, effect=GrantEffect.ALLOW)],
        rules=[],
        requested_scope=requested,
    )


def test_path_glob_grants_match_requests() -> None:
    decision = _decision(
        "http.get",
        {"connection_id": CONNECTION, "path": "/v1/*"},
        {"connection_id": CONNECTION, "method": "GET", "path": "/v1/items"},
    )
    assert decision.decision.value == "allow"


def test_path_outside_grant_is_denied() -> None:
    decision = _decision(
        "http.get",
        {"connection_id": CONNECTION, "path": "/v1/*"},
        {"connection_id": CONNECTION, "method": "GET", "path": "/admin"},
    )
    assert decision.decision.value == "deny"
    assert decision.code == "scope_mismatch"


def test_method_restriction_is_enforced() -> None:
    allowed = _decision(
        "http.request",
        {"connection_id": CONNECTION, "method": "POST"},
        {"connection_id": CONNECTION, "method": "POST", "path": "/v1/items"},
    )
    denied = _decision(
        "http.request",
        {"connection_id": CONNECTION, "method": "POST"},
        {"connection_id": CONNECTION, "method": "DELETE", "path": "/v1/items"},
    )
    assert allowed.decision.value != "deny"
    assert denied.decision.value == "deny"


def test_capability_without_grant_is_denied() -> None:
    decision = evaluate(
        _definition("http.request"),
        grants=[],
        rules=[],
        requested_scope={"connection_id": CONNECTION, "method": "POST", "path": "/v1"},
    )
    assert decision.decision.value == "deny"
    assert decision.code == "no_grant"
