"""verify_connection and validate_settings for the web connector."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

import jhin_connectors.web.connector as web_connector_module
from jhin_connectors.base import VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.web.connector import WebConnector

BASE = "http://fake-websearch:8080"


@pytest.fixture
def allow_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", BASE)


def _mock_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def factory(headers=None):  # type: ignore[no-untyped-def]
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            headers=dict(headers or {}),
        )

    monkeypatch.setattr(web_connector_module, "http_client", factory)


def _ctx(backend: str = "tavily") -> VerifyContext:
    return VerifyContext(
        auth_type="bearer",
        credentials={"token": "tok"},
        config={"search_backend": backend, "base_url": BASE},
    )


async def test_verify_runs_one_cheap_search(monkeypatch, allow_fake):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    _mock_client(monkeypatch, handler)
    health = await WebConnector().verify_connection(_ctx())
    assert health.ok is True
    assert health.details["backend"] == "tavily"
    body = seen[0].content
    assert b'"max_results": 1' in body or b'"max_results":1' in body


async def test_verify_reports_rejected_keys_and_server_errors(monkeypatch, allow_fake):  # type: ignore[no-untyped-def]
    _mock_client(monkeypatch, lambda request: httpx.Response(401, json={"detail": "no"}))
    unauthorized = await WebConnector().verify_connection(_ctx("brave"))
    assert unauthorized.ok is False
    assert "rejected the key" in unauthorized.message

    _mock_client(monkeypatch, lambda request: httpx.Response(503))
    unhealthy = await WebConnector().verify_connection(_ctx("exa"))
    assert unhealthy.ok is False
    assert "503" in unhealthy.message

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _mock_client(monkeypatch, broken)
    unreachable = await WebConnector().verify_connection(_ctx())
    assert unreachable.ok is False
    assert "ConnectError" in unreachable.message


async def test_verify_fetch_only_is_a_pure_policy_check() -> None:
    health = await WebConnector().verify_connection(
        VerifyContext(auth_type="none", credentials={}, config={})
    )
    assert health.ok is True
    assert health.details == {"mode": "fetch_only"}

    bad = await WebConnector().verify_connection(
        VerifyContext(auth_type="none", credentials={}, config={"allowed_domains": ["bad host"]})
    )
    assert bad.ok is False


async def test_verify_rejects_bad_backend_without_probing() -> None:
    health = await WebConnector().verify_connection(
        VerifyContext(
            auth_type="bearer", credentials={"token": "tok"}, config={"search_backend": "google"}
        )
    )
    assert health.ok is False
    assert "search_backend" in health.message


def test_validate_settings_normalizes_and_fails_closed(allow_fake) -> None:  # type: ignore[no-untyped-def]
    connector = WebConnector()
    validated = connector.validate_settings(
        "bearer",
        {"search_backend": " TAVILY ", "base_url": BASE, "allowed_domains": ["Docs.Example.com"]},
    )
    assert validated["search_backend"] == "tavily"
    assert validated["allowed_domains"] == ["docs.example.com"]

    with pytest.raises(EndpointPolicyError):
        connector.validate_settings("bearer", {"search_backend": "google"})
    with pytest.raises(EndpointPolicyError):
        connector.validate_settings(
            "bearer", {"search_backend": "tavily", "base_url": "http://not-allowed:9"}
        )

    # Fetch-only connections drop the search fields they cannot use.
    fetch_only = connector.validate_settings("none", {"base_url": BASE})
    assert "base_url" not in fetch_only
    empty = connector.validate_settings("none", {"allowed_domains": []})
    assert "allowed_domains" not in empty
