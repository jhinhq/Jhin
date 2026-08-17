"""Webhook signature verification and event normalization with fixture
payloads shaped like real GitHub deliveries."""

import json

from jhin_connectors.base import RawWebhookEvent
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.webhook import normalize, sign_payload, verify_signature

SECRET = "whsec_1234567890"
BODY = json.dumps({"action": "opened", "number": 7}).encode()


# --- signature verification (plan 48.5) ---


def test_valid_signature_accepted() -> None:
    assert verify_signature(SECRET, BODY, sign_payload(SECRET, BODY))


def test_wrong_secret_rejected() -> None:
    assert not verify_signature(SECRET, BODY, sign_payload("other-secret", BODY))


def test_tampered_body_rejected() -> None:
    signature = sign_payload(SECRET, BODY)
    assert not verify_signature(SECRET, BODY + b" ", signature)


def test_missing_or_malformed_header_rejected() -> None:
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, "")
    assert not verify_signature(SECRET, BODY, "sha256=")
    assert not verify_signature(SECRET, BODY, "sha1=abcdef")


def test_empty_secret_never_verifies() -> None:
    assert not verify_signature("", BODY, sign_payload("", BODY))


# --- normalization fixtures (plan 11.2 events) ---


def _raw(event: str, payload: dict) -> RawWebhookEvent:
    return RawWebhookEvent(event=event, delivery_id="d-1", payload=payload)


REPO = {"full_name": "octo/alpha"}
SENDER = {"login": "octocat"}


def test_normalize_issue_opened() -> None:
    events = normalize(
        _raw(
            "issues",
            {
                "action": "opened",
                "repository": REPO,
                "sender": SENDER,
                "issue": {
                    "number": 12,
                    "title": "Login fails",
                    "state": "open",
                    "user": {"login": "reporter"},
                },
            },
        )
    )
    assert len(events) == 1
    assert events[0].event_type == "connector.github.issue.opened"
    assert events[0].data["repository"] == "octo/alpha"
    assert events[0].data["number"] == 12
    assert events[0].data["author"] == "reporter"
    assert events[0].data["sender"] == "octocat"


def test_normalize_pull_request_opened() -> None:
    events = normalize(
        _raw(
            "pull_request",
            {
                "action": "opened",
                "repository": REPO,
                "sender": SENDER,
                "pull_request": {
                    "number": 3,
                    "title": "Fix login",
                    "state": "open",
                    "merged": False,
                    "user": {"login": "octocat"},
                    "head": {"ref": "agent/fix-login"},
                    "base": {"ref": "main"},
                },
            },
        )
    )
    assert len(events) == 1
    assert events[0].event_type == "connector.github.pull_request.opened"
    assert events[0].data["head"] == "agent/fix-login"
    assert events[0].data["base"] == "main"
    assert events[0].data["merged"] is False


def test_normalize_push() -> None:
    events = normalize(
        _raw(
            "push",
            {
                "repository": REPO,
                "sender": SENDER,
                "ref": "refs/heads/main",
                "before": "a" * 40,
                "after": "b" * 40,
                "commits": [{"id": "b" * 40}],
            },
        )
    )
    assert len(events) == 1
    assert events[0].event_type == "connector.github.push"
    assert events[0].data["ref"] == "refs/heads/main"
    assert events[0].data["commit_count"] == 1


def test_normalize_check_suite_and_workflow_run() -> None:
    check = normalize(
        _raw(
            "check_suite",
            {
                "action": "completed",
                "repository": REPO,
                "check_suite": {
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": "c" * 40,
                },
            },
        )
    )
    assert check[0].event_type == "connector.github.check_suite.completed"
    assert check[0].data["conclusion"] == "success"

    run = normalize(
        _raw(
            "workflow_run",
            {
                "action": "completed",
                "repository": REPO,
                "workflow_run": {
                    "id": 99,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "main",
                    "run_number": 41,
                },
            },
        )
    )
    assert run[0].event_type == "connector.github.workflow_run.completed"
    assert run[0].data["run_id"] == 99
    assert run[0].data["conclusion"] == "failure"


def test_unknown_and_malformed_events_normalize_to_nothing() -> None:
    assert normalize(_raw("deployment", {"repository": REPO})) == []
    assert normalize(_raw("issues", {"action": "opened"})) == []  # no issue object
    # A hostile action string cannot become a subject token.
    assert (
        normalize(_raw("issues", {"action": "opened.evil injected", "issue": {"number": 1}})) == []
    )


def test_connector_exposes_webhook_events() -> None:
    connector = GitHubConnector()
    assert set(connector.webhook_events()) == {
        "issues",
        "pull_request",
        "check_suite",
        "workflow_run",
        "push",
    }
