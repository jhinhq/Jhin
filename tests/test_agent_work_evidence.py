"""Retained evidence verification uses only bounded authenticated reads."""

from copy import deepcopy
from typing import Any
from uuid import UUID

import httpx
import pytest
from scripts.verify_agent_work_evidence import (
    EvidenceClient,
    EvidenceError,
    verify_editorial,
    verify_key_only,
)


def fixture() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    def item(kind: str, identity: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"id": f"{kind}:{identity}", "data": {"id": identity, **data}}

    def call(
        identity: str,
        name: str,
        actor: str,
        minute: int,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return item(
            "tool_call",
            identity,
            {
                "tool_name": name,
                "agent_id": actor,
                "task_id": "parent" if actor == "writer" else "child",
                "status": "completed",
                "created_at": f"2026-09-12T12:{minute:02d}:00Z",
                "sanitized_input_json": args,
                "sanitized_output_json": result,
            },
        )

    review = {
        "review_id": "review",
        "post_id": "post",
        "revision": "revision",
        "work_request_id": "handoff",
        "connection_id": "connection",
        "author_agent_id": "writer",
        "publisher_agent_id": "director",
        "status": "published",
    }
    items = [
        call(
            "draft",
            "ghost.draft.create",
            "writer",
            0,
            {},
            {"post_id": "post", "revision": "revision"},
        ),
        call(
            "request",
            "ghost.review.request",
            "writer",
            1,
            {"expected_revision": "revision", "connection_id": "connection"},
            {**review, "created_task_id": "child"},
        ),
        call(
            "decision",
            "ghost.review.decide",
            "director",
            2,
            {"review_id": "review", "verdict": "approved"},
            {"revision": "revision"},
        ),
        call(
            "publication",
            "ghost.post.publish",
            "director",
            3,
            {"review_id": "review", "connection_id": "connection"},
            {"post_id": "post", "status": "published", "revision": "published-updated-at-revision"},
        ),
        item("work_request", "handoff", {"target_agent_id": "director", "status": "completed"}),
        item(
            "message",
            "returned",
            {
                "task_id": "parent",
                "sender_id": "director",
                "message_type": "result",
                "content_json": {"work_request_id": "handoff"},
                "created_at": "2026-09-12T12:04:00Z",
            },
        ),
        item(
            "message",
            "final",
            {
                "task_id": "parent",
                "sender_id": "writer",
                "message_type": "text",
                "created_at": "2026-09-12T12:05:00Z",
            },
        ),
    ]
    return (
        {"tasks": [{"id": "parent", "state": "completed"}, {"id": "child", "state": "completed"}]},
        items,
        review,
    )


def test_editorial_verifier_accepts_exact_review_and_returned_handoff() -> None:
    assert verify_editorial(*fixture())["parent_completed"] is True


def approved_draft_fixture() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    detail, items, review = fixture()
    review.update(status="approved", release_intent="draft_only")
    items[3]["data"].update(tool_name="ghost.post.read", agent_id="writer")
    items[3]["data"]["sanitized_input_json"] = {"connection_id": "connection", "post_id": "post"}
    items[3]["data"]["sanitized_output_json"] = {
        "post_id": "post",
        "status": "draft",
        "revision": "revision",
    }
    return detail, items, review


def test_verifier_accepts_approved_draft_with_independent_readback() -> None:
    assert verify_editorial(*approved_draft_fixture())["outcome"] == "approved_draft"


def test_draft_verifier_rejects_publish_attempt_even_if_failed() -> None:
    detail, items, review = approved_draft_fixture()
    attempt = deepcopy(items[3])
    attempt["id"] = "tool_call:publish_attempt"
    attempt["data"].update(tool_name="ghost.post.publish", status="failed")
    items.append(attempt)
    with pytest.raises(EvidenceError, match="publish"):
        verify_editorial(detail, items, review)


def test_verifier_accepts_substantive_revision_rounds() -> None:
    detail, items, final_review = approved_draft_fixture()
    prior_review = {
        **final_review,
        "review_id": "prior",
        "work_request_id": "prior-work",
        "revision": "prior-revision",
        "status": "changes_requested",
    }
    previous = deepcopy(items)
    previous.pop(3)  # no readback required for the first changes-requested outcome
    for item in previous:
        item["id"] += "-prior"
        data = item["data"]
        for field in ("sanitized_input_json", "sanitized_output_json", "content_json"):
            value = data.get(field, {})
            for key, before, after in (
                ("review_id", "review", "prior"),
                ("work_request_id", "handoff", "prior-work"),
                ("revision", "revision", "prior-revision"),
                ("expected_revision", "revision", "prior-revision"),
            ):
                if value.get(key) == before:
                    value[key] = after
            if value.get("verdict") == "approved":
                value["verdict"] = "changes_requested"
        if data.get("id") == "handoff":
            data["id"] = "prior-work"
    for item in items:
        data = item["data"]
        if "created_at" in data:
            data["created_at"] = data["created_at"].replace("T12:", "T13:")
    items[0]["data"]["tool_name"] = "ghost.draft.update"
    final_review["prior_review_id"] = "prior"
    report = verify_editorial(detail, previous + items, [prior_review, final_review])
    assert report["review_rounds"] == 2
    assert report["outcome"] == "approved_draft"


@pytest.mark.parametrize(
    "change",
    [
        "writer_publishes",
        "wrong_revision",
        "other_post",
        "child_unfinished",
        "missing_result",
        "duplicate_handoff",
    ],
)
def test_editorial_verifier_rejects_incomplete_or_mismatched_evidence(change: str) -> None:
    detail, items, review = fixture()
    if change == "writer_publishes":
        items[3]["data"]["agent_id"] = "writer"
    elif change == "wrong_revision":
        review["revision"] = "changed"
    elif change == "other_post":
        items[3]["data"]["sanitized_output_json"]["post_id"] = "another"
    elif change == "child_unfinished":
        detail["tasks"][1]["state"] = "running"
    elif change == "missing_result":
        items.pop(5)
    elif change == "duplicate_handoff":
        items.append(deepcopy(items[4]))
    with pytest.raises(EvidenceError):
        verify_editorial(detail, items, review)


def test_key_only_verifier_refuses_effects_before_answer() -> None:
    question: dict[str, Any] = {
        "id": "user_question:q",
        "data": {
            "id": "q",
            "task_id": "task",
            "required": True,
            "input_key": "ghost_admin_url",
            "asked_at": "2026-09-12T12:00:00Z",
            "answered_at": None,
        },
    }
    assert verify_key_only([question])["external_calls_before_answer"] == 0
    effect = {
        "id": "tool_call:effect",
        "data": {
            "id": "effect",
            "task_id": "task",
            "tool_name": "ghost.post.list",
            "status": "completed",
            "created_at": "2026-09-12T12:01:00Z",
        },
    }
    with pytest.raises(EvidenceError):
        verify_key_only([question, effect])
    question["data"]["answered_at"] = "2026-09-12T12:02:00Z"
    with pytest.raises(EvidenceError):
        verify_key_only([question, effect])
    question["data"]["answered_at"] = "2026-09-12T12:00:30Z"
    assert verify_key_only([question, effect])["answered"] is True


def test_client_only_reads_and_stops_when_pagination_changes() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        if not request.url.path.endswith("/items"):
            return httpx.Response(200, json={"tasks": []})
        second = "before" in request.url.params
        return httpx.Response(
            200,
            json={
                "cursor": 2 if second else 1,
                "items": [{"id": "message:one", "data": {}}],
                "has_more": not second,
                "next_before": 1,
            },
        )

    with (
        httpx.Client(
            base_url="http://localhost",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer synthetic-key"},
        ) as client,
        pytest.raises(EvidenceError, match="changed"),
    ):
        EvidenceClient(client, UUID(int=1)).snapshot(UUID(int=2))
    assert seen == ["GET", "GET", "GET"]
