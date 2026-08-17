"""The fake Linear admin surface: signed webhook firing, redelivery, refire.

These tests stand up a tiny local HTTP sink, point the fake Linear at it,
and verify that fired deliveries pass the real connector's parse_webhook —
i.e. the fake signs exactly like Linear does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from jhin_connectors.linear.connector import LinearConnector
from jhin_connectors.testing.fake_linear import FakeLinearServer

SECRET = "sink-webhook-secret"

connector = LinearConnector()


class _Sink:
    """Collects webhook deliveries (headers + raw body)."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[dict[str, str], bytes]] = []
        self._lock = threading.Lock()

        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with sink._lock:
                    sink.deliveries.append((dict(self.headers.items()), body))
                data = b'{"status": "accepted"}'
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def __enter__(self) -> _Sink:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stack() -> Iterator[tuple[FakeLinearServer, _Sink]]:
    with FakeLinearServer() as fake, _Sink() as sink:
        httpx.post(f"{fake.base_url}/_admin/webhook", json={"url": sink.url, "secret": SECRET})
        yield fake, sink


def test_transition_fires_verifiable_webhook(stack: tuple[FakeLinearServer, _Sink]) -> None:
    fake, sink = stack
    response = httpx.post(
        f"{fake.base_url}/_admin/issues/ENG-142/transition", json={"state": "Todo"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True

    headers, body = sink.deliveries[0]
    raw = connector.parse_webhook(headers, body, SECRET)  # signature + timestamp verified
    assert raw.event == "Issue"
    payload = raw.payload
    assert payload["action"] == "update"
    assert payload["data"]["identifier"] == "ENG-142"
    assert payload["data"]["state"]["name"] == "Todo"
    # updatedFrom carries the previous state id — the transition metadata.
    backlog_id = next(
        state["id"] for state in fake.state.teams["ENG"]["states"] if state["name"] == "Backlog"
    )
    assert payload["updatedFrom"]["stateId"] == backlog_id


def test_transition_to_current_state_conflicts(stack: tuple[FakeLinearServer, _Sink]) -> None:
    fake, _ = stack
    assert (
        httpx.post(
            f"{fake.base_url}/_admin/issues/ENG-142/transition", json={"state": "Backlog"}
        ).status_code
        == 409
    )


def test_edit_fires_webhook_without_state_change(stack: tuple[FakeLinearServer, _Sink]) -> None:
    fake, sink = stack
    response = httpx.post(
        f"{fake.base_url}/_admin/issues/ENG-142/edit", json={"title": "Renamed ticket"}
    )
    assert response.status_code == 200
    _, body = sink.deliveries[0]
    payload = json.loads(body)
    assert payload["data"]["title"] == "Renamed ticket"
    assert "stateId" not in payload["updatedFrom"]
    assert payload["updatedFrom"]["title"] == "Fix the failing unit test in octo/alpha"


def test_redeliver_is_byte_identical(stack: tuple[FakeLinearServer, _Sink]) -> None:
    fake, sink = stack
    first = httpx.post(
        f"{fake.base_url}/_admin/issues/ENG-142/transition", json={"state": "Todo"}
    ).json()
    redelivered = httpx.post(
        f"{fake.base_url}/_admin/redeliver", json={"delivery_id": first["delivery_id"]}
    ).json()
    assert redelivered["delivery_id"] == first["delivery_id"]

    (headers_1, body_1), (headers_2, body_2) = sink.deliveries
    assert body_1 == body_2
    assert headers_1["Linear-Delivery"] == headers_2["Linear-Delivery"]
    assert headers_1["Linear-Signature"] == headers_2["Linear-Signature"]


def test_refire_is_new_delivery_same_content(stack: tuple[FakeLinearServer, _Sink]) -> None:
    fake, sink = stack
    first = httpx.post(
        f"{fake.base_url}/_admin/issues/ENG-142/transition", json={"state": "Todo"}
    ).json()
    refired = httpx.post(
        f"{fake.base_url}/_admin/refire", json={"delivery_id": first["delivery_id"]}
    ).json()
    assert refired["delivery_id"] != first["delivery_id"]

    (headers_1, body_1), (headers_2, body_2) = sink.deliveries
    payload_1, payload_2 = json.loads(body_1), json.loads(body_2)
    assert headers_1["Linear-Delivery"] != headers_2["Linear-Delivery"]
    assert payload_1["data"] == payload_2["data"]
    assert payload_1["updatedFrom"] == payload_2["updatedFrom"]
    # Both deliveries independently pass verification.
    for headers, body in sink.deliveries:
        raw = connector.parse_webhook(headers, body, SECRET)
        assert raw.payload["data"]["state"]["name"] == "Todo"


def test_graphql_issue_update_fires_webhook(stack: tuple[FakeLinearServer, _Sink]) -> None:
    """State changes made through the GraphQL API also emit webhooks —
    matching real Linear, where agent-made updates round-trip as events."""
    fake, sink = stack
    todo_id = next(
        state["id"] for state in fake.state.teams["ENG"]["states"] if state["name"] == "Todo"
    )
    issue_id = fake.state.issues["ENG-142"]["id"]
    response = httpx.post(
        f"{fake.base_url}/graphql",
        headers={"Authorization": "fake-linear-api-key"},
        json={
            "query": (
                "mutation($id: String!, $input: IssueUpdateInput!) "
                "{ issueUpdate(id: $id, input: $input) { success issue { id } } }"
            ),
            "variables": {"id": issue_id, "input": {"stateId": todo_id}},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["issueUpdate"]["success"] is True
    _, body = sink.deliveries[0]
    payload = json.loads(body)
    assert payload["updatedFrom"]["stateId"]
    assert payload["data"]["state"]["name"] == "Todo"
