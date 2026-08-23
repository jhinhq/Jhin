"""verify_connection and validate_settings for the generic HTTP connector."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

import jhin_connectors.http.connector as http_connector_module
from jhin_connectors.base import VerifyContext
from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.http.connector import HttpConnector

BASE = "http://fake-api:8080"


@pytest.fixture
def allow_fake_api(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(http_connector_module, "http_client", factory)


def _ctx(auth_type: str = "none", credentials: dict[str, str] | None = None) -> VerifyContext:
    return VerifyContext(
        auth_type=auth_type, credentials=credentials or {}, config={"base_url": BASE}
    )


async def test_verify_reports_reachable_on_2xx(monkeypatch, allow_fake_api):  # type: ignore[no-untyped-def]
    _mock_client(monkeypatch, lambda request: httpx.Response(204))
    health = await HttpConnector().verify_connection(_ctx())
    assert health.ok is True
    assert health.details == {"status": "204"}
    assert "204" in health.message


async def test_verify_accepts_4xx_and_reports_status(monkeypatch, allow_fake_api):  # type: ignore[no-untyped-def]
    _mock_client(monkeypatch, lambda request: httpx.Response(401))
    health = await HttpConnector().verify_connection(
        _ctx(auth_type="bearer", credentials={"token": "tok"})
    )
    assert health.ok is True
    assert health.details == {"status": "401"}


async def test_verify_falls_back_to_get_when_head_unsupported(monkeypatch, allow_fake_api):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405 if request.method == "HEAD" else 200)

    _mock_client(monkeypatch, handler)
    health = await HttpConnector().verify_connection(_ctx())
    assert health.ok is True
    assert health.details == {"status": "200"}


async def test_verify_fails_on_5xx_and_transport_errors(monkeypatch, allow_fake_api):  # type: ignore[no-untyped-def]
    _mock_client(monkeypatch, lambda request: httpx.Response(503))
    unhealthy = await HttpConnector().verify_connection(_ctx())
    assert unhealthy.ok is False
    assert "503" in unhealthy.message

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _mock_client(monkeypatch, broken)
    unreachable = await HttpConnector().verify_connection(_ctx())
    assert unreachable.ok is False
    assert "ConnectError" in unreachable.message


async def test_verify_rejects_disallowed_base_url_without_probing() -> None:
    health = await HttpConnector().verify_connection(
        VerifyContext(auth_type="none", credentials={}, config={"base_url": "http://internal:80"})
    )
    assert health.ok is False
    assert "not allowed" in health.message


def test_validate_settings_normalizes_base_url_and_checks_headers(allow_fake_api) -> None:  # type: ignore[no-untyped-def]
    connector = HttpConnector()
    validated = connector.validate_settings(
        "header",
        {"base_url": BASE, "header_name": "X-API-Key", "default_headers": ["X-Env: dev"]},
    )
    assert validated["base_url"] == f"{BASE}/"
    with pytest.raises(EndpointPolicyError):
        connector.validate_settings("none", {"base_url": "http://not-allowed:9"})
    with pytest.raises(EndpointPolicyError, match="reserved"):
        connector.validate_settings("header", {"base_url": BASE, "header_name": "Cookie"})
    with pytest.raises(EndpointPolicyError, match="Name: value"):
        connector.validate_settings("none", {"base_url": BASE, "default_headers": ["broken"]})
