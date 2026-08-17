"""Linear webhook verification + normalization (plan 11.3, 10.4, 48.5)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from jhin_connectors.base import RawWebhookEvent, WebhookVerificationError
from jhin_connectors.linear.connector import LinearConnector
from jhin_connectors.linear.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    changed_from,
    normalize,
    sign_payload,
    timestamp_is_fresh,
    verify_signature,
)

SECRET = "test-webhook-secret"

connector = LinearConnector()


def _issue_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "update",
        "type": "Issue",
        "webhookTimestamp": int(time.time() * 1000),
        "url": "https://linear.fake/jhin/issue/ENG-142",
        "data": {
            "id": "issue-uuid-1",
            "identifier": "ENG-142",
            "title": "Fix the failing unit test",
            "description": "Make run_tests.sh pass.",
            "priority": 2,
            "team": {"id": "team-1", "key": "ENG", "name": "Engineering"},
            "state": {"id": "state-todo", "name": "Todo", "type": "unstarted"},
            "labels": [{"name": "bug"}],
        },
        "updatedFrom": {"updatedAt": "2026-08-17T00:00:00.000Z", "stateId": "state-backlog"},
    }
    payload.update(overrides)
    return payload


def _headers(body: bytes, *, event: str = "Issue", secret: str = SECRET) -> dict[str, str]:
    return {
        SIGNATURE_HEADER: sign_payload(secret, body),
        EVENT_HEADER: event,
        DELIVERY_HEADER: "delivery-1",
    }


# --- signature ---


def test_signature_is_bare_hex_hmac() -> None:
    body = b'{"a": 1}'
    signature = sign_payload(SECRET, body)
    assert len(signature) == 64 and not signature.startswith("sha256=")
    assert verify_signature(SECRET, body, signature)
    assert verify_signature(SECRET, body, f"  {signature}  ")  # tolerant of whitespace


def test_signature_rejections() -> None:
    body = b'{"a": 1}'
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, sign_payload("other-secret", body))
    assert not verify_signature("", body, sign_payload("", body))  # no secret, never valid


def test_parse_webhook_rejects_bad_signature_before_parsing() -> None:
    body = b"this is not even json"
    headers = {SIGNATURE_HEADER: "0" * 64, EVENT_HEADER: "Issue", DELIVERY_HEADER: "d1"}
    with pytest.raises(WebhookVerificationError, match="Linear-Signature"):
        connector.parse_webhook(headers, body, SECRET)


def test_parse_webhook_requires_headers() -> None:
    body = json.dumps(_issue_payload()).encode()
    headers = _headers(body)
    del headers[DELIVERY_HEADER]
    with pytest.raises(WebhookVerificationError, match="Linear-Delivery"):
        connector.parse_webhook(headers, body, SECRET)


# --- timestamp replay guard ---


def test_timestamp_freshness() -> None:
    now = time.time()
    assert timestamp_is_fresh({"webhookTimestamp": int(now * 1000)}, now=now)
    assert timestamp_is_fresh({"webhookTimestamp": int((now - 59) * 1000)}, now=now)
    assert not timestamp_is_fresh({"webhookTimestamp": int((now - 61) * 1000)}, now=now)
    assert not timestamp_is_fresh({"webhookTimestamp": int((now + 61) * 1000)}, now=now)
    assert not timestamp_is_fresh({}, now=now)
    assert not timestamp_is_fresh({"webhookTimestamp": "yesterday"}, now=now)
    assert not timestamp_is_fresh({"webhookTimestamp": True}, now=now)


def test_parse_webhook_rejects_stale_timestamp() -> None:
    payload = _issue_payload(webhookTimestamp=int((time.time() - 300) * 1000))
    body = json.dumps(payload).encode()
    with pytest.raises(WebhookVerificationError, match="webhookTimestamp"):
        connector.parse_webhook(_headers(body), body, SECRET)


def test_parse_webhook_accepts_valid_delivery() -> None:
    payload = _issue_payload()
    body = json.dumps(payload).encode()
    raw = connector.parse_webhook(_headers(body), body, SECRET)
    assert raw.event == "Issue"
    assert raw.delivery_id == "delivery-1"
    assert raw.payload["data"]["identifier"] == "ENG-142"


# --- normalization ---


def test_normalize_issue_update_preserves_transition_metadata() -> None:
    raw = RawWebhookEvent(event="Issue", delivery_id="d1", payload=_issue_payload())
    events = normalize(raw)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "connector.linear.issue.updated"
    assert event.data["external_id"] == "ENG-142"
    assert event.data["team"] == {"id": "team-1", "key": "ENG", "name": "Engineering"}
    assert event.data["state"] == {"id": "state-todo", "name": "Todo", "type": "unstarted"}
    # updatedFrom.stateId is mirrored into the nested changed_from shape so
    # trigger filters can detect the transition (plan 10.4).
    assert event.data["changed_from"] == {"state": {"id": "state-backlog"}}
    assert event.data["labels"] == ["bug"]


def test_normalize_title_edit_has_no_state_change() -> None:
    payload = _issue_payload(
        updatedFrom={"updatedAt": "2026-08-17T00:00:00.000Z", "title": "Old title"}
    )
    events = normalize(RawWebhookEvent(event="Issue", delivery_id="d1", payload=payload))
    assert events[0].data["changed_from"] == {"title": "Old title"}
    assert "state" not in events[0].data["changed_from"]


def test_normalize_issue_create_has_no_changed_from() -> None:
    payload = _issue_payload(action="create")
    payload.pop("updatedFrom")
    events = normalize(RawWebhookEvent(event="Issue", delivery_id="d1", payload=payload))
    assert events[0].event_type == "connector.linear.issue.created"
    assert "changed_from" not in events[0].data


def test_changed_from_mirrors_known_fields_and_drops_noise() -> None:
    assert changed_from(
        {
            "stateId": "s1",
            "teamId": "t1",
            "assigneeId": "a1",
            "title": "Old",
            "priority": 3,
            "updatedAt": "x",
            "sortOrder": 12.5,
            "somethingElse": "kept",
        }
    ) == {
        "state": {"id": "s1"},
        "team": {"id": "t1"},
        "assignee": {"id": "a1"},
        "title": "Old",
        "priority": 3,
        "somethingElse": "kept",
    }
    assert changed_from(None) == {}
    assert changed_from("junk") == {}


def test_normalize_comment_created() -> None:
    payload = {
        "action": "create",
        "type": "Comment",
        "webhookTimestamp": int(time.time() * 1000),
        "url": "https://linear.fake/jhin/issue/ENG-142#comment-1",
        "data": {
            "id": "comment-1",
            "body": "Looks good",
            "issueId": "issue-uuid-1",
            "issue": {"id": "issue-uuid-1", "identifier": "ENG-142"},
            "userId": "user-1",
        },
    }
    events = normalize(RawWebhookEvent(event="Comment", delivery_id="d2", payload=payload))
    assert len(events) == 1
    assert events[0].event_type == "connector.linear.comment.created"
    assert events[0].data["issue_identifier"] == "ENG-142"
    assert events[0].data["external_id"] == "comment-1"


def test_normalize_ignores_unknown_shapes() -> None:
    assert normalize(RawWebhookEvent(event="Project", delivery_id="d", payload={})) == []
    assert (
        normalize(RawWebhookEvent(event="Issue", delivery_id="d", payload={"action": "weird"}))
        == []
    )
    assert (
        normalize(RawWebhookEvent(event="Issue", delivery_id="d", payload={"action": "update"}))
        == []
    )  # no identifier
    assert (
        normalize(RawWebhookEvent(event="Comment", delivery_id="d", payload={"action": "update"}))
        == []
    )  # only comment creation normalizes
