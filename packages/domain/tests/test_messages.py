"""Structured message content builders (plan 29)."""

from __future__ import annotations

from jhin_domain import AGENT_MESSAGE_TYPES, MessageType, artifact, structured_content


def test_agent_message_types_cover_plan_29() -> None:
    assert {t.value for t in AGENT_MESSAGE_TYPES} == {
        "instruction",
        "question",
        "status",
        "result",
        "delegation",
        "review_request",
        "review_result",
        "escalation",
    }
    assert MessageType.TOOL_CALL not in AGENT_MESSAGE_TYPES


def test_structured_content_canonical_keys_always_present() -> None:
    content = structured_content("Implemented token rotation and opened PR #381.")
    assert content == {
        "summary": "Implemented token rotation and opened PR #381.",
        "artifacts": [],
        "risks": [],
        "recommended_next_action": "",
    }


def test_structured_content_with_artifacts_extras_and_caps() -> None:
    content = structured_content(
        "s" * 10_000,
        artifacts=[artifact("github_pull_request", id="381", url_ref="http://gh/381"), "junk"],
        risks=["flaky test", 42],
        recommended_next_action="delegate_to_qa",
        task_id="t-1",
        status="completed",
    )
    assert len(content["summary"]) == 4_000
    assert content["artifacts"] == [
        {"type": "github_pull_request", "id": "381", "url_ref": "http://gh/381"}
    ]
    assert content["risks"] == ["flaky test", "42"]
    assert content["recommended_next_action"] == "delegate_to_qa"
    assert content["task_id"] == "t-1"
    assert content["status"] == "completed"


def test_malformed_collections_normalize_to_empty() -> None:
    content = structured_content("s", artifacts="not-a-list", risks={"not": "a list"})
    assert content["artifacts"] == []
    assert content["risks"] == []
