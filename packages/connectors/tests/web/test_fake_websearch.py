"""Contract tests: the real web-connector executors against the in-process
fake web-search server (the same double the dev stack runs)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from jhin_connectors.testing.fake_websearch import DEFAULT_TOKEN, FakeWebSearchServer
from jhin_connectors.web.connector import WebConnector
from jhin_connectors.web.schemas import (
    WebFetchInput,
    WebFetchOutput,
    WebSearchInput,
    WebSearchOutput,
)

EXECUTORS = {definition.name: executor for definition, executor in WebConnector().tools()}


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeWebSearchServer]:
    with FakeWebSearchServer() as fake:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", fake.base_url)
        yield fake


@pytest.mark.parametrize("backend", ["tavily", "brave", "exa"])
async def test_search_speaks_every_backend_shape(
    backend, server, workspace, make_connection, context
):  # type: ignore[no-untyped-def]
    connection = await make_connection(
        workspace,
        connector_type="web",
        name=f"Fake web {backend}",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"search_backend": backend, "base_url": server.base_url},
    )
    output = await EXECUTORS["web.search"](
        context,
        WebSearchInput(connection_id=str(connection.id), query="jhin agents", max_results=2),
    )
    assert isinstance(output, WebSearchOutput)
    assert output.backend == backend
    assert len(output.results) == 2
    assert all(result.url.startswith(server.base_url) for result in output.results)
    assert "jhin agents" in output.results[0].title
    assert server.state.snapshot()["searches"][backend] == 1


async def test_fetch_reads_a_served_page_and_strips_scripts(
    server, workspace, make_connection, context
):  # type: ignore[no-untyped-def]
    connection = await make_connection(
        workspace,
        connector_type="web",
        name="Fake web fetch",
        auth_type="bearer",
        credentials={"token": DEFAULT_TOKEN},
        config={"search_backend": "tavily", "base_url": server.base_url},
    )
    output = await EXECUTORS["web.fetch"](
        context,
        WebFetchInput(connection_id=str(connection.id), url=f"{server.base_url}/pages/jhin"),
    )
    assert isinstance(output, WebFetchOutput)
    assert output.title == "About Jhin"
    assert "deny-by-default connectors" in output.text
    assert "never reach extracted output" not in output.text

    redirected = await EXECUTORS["web.fetch"](
        context,
        WebFetchInput(connection_id=str(connection.id), url=f"{server.base_url}/pages/redirect"),
    )
    assert isinstance(redirected, WebFetchOutput)
    assert redirected.status_code == 200
    assert redirected.final_url.endswith("/pages/jhin")

    offsite = await EXECUTORS["web.fetch"](
        context,
        WebFetchInput(connection_id=str(connection.id), url=f"{server.base_url}/pages/offsite"),
    )
    assert isinstance(offsite, WebFetchOutput)
    assert offsite.status_code == 302

    huge = await EXECUTORS["web.fetch"](
        context,
        WebFetchInput(connection_id=str(connection.id), url=f"{server.base_url}/pages/huge"),
    )
    assert isinstance(huge, WebFetchOutput)
    assert huge.truncated is True


async def test_verify_and_wrong_token_against_the_fake(server) -> None:  # type: ignore[no-untyped-def]
    from jhin_connectors.base import VerifyContext

    connector = WebConnector()
    ok = await connector.verify_connection(
        VerifyContext(
            auth_type="bearer",
            credentials={"token": DEFAULT_TOKEN},
            config={"search_backend": "brave", "base_url": server.base_url},
        )
    )
    assert ok.ok is True

    rejected = await connector.verify_connection(
        VerifyContext(
            auth_type="bearer",
            credentials={"token": "wrong-token"},
            config={"search_backend": "brave", "base_url": server.base_url},
        )
    )
    assert rejected.ok is False
    assert "rejected the key" in rejected.message
