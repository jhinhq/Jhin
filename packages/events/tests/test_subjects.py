import pytest

from jhin_events.subjects import (
    dlq_subject,
    event_subject,
    ingress_subject,
)


def test_event_subject_canonical_form() -> None:
    assert event_subject("ws-1", "task.created") == "jhin.v1.ws-1.task.created"
    assert (
        event_subject("ws-1", "connector.linear.issue.updated")
        == "jhin.v1.ws-1.connector.linear.issue.updated"
    )


def test_ingress_and_dlq_subjects() -> None:
    assert ingress_subject("ws-1", "linear", "issue_updated") == (
        "jhin.v1.ws-1.ingress.linear.issue_updated"
    )
    assert dlq_subject("EVENTS") == "jhin.dlq.events"


def test_dotted_ingress_event_becomes_individual_subject_tokens() -> None:
    assert ingress_subject("w1", "vercel", "deployment.ready") == (
        "jhin.v1.w1.ingress.vercel.deployment.ready"
    )


@pytest.mark.parametrize("event", [".ready", "deployment.", "deployment..ready"])
def test_ingress_subject_rejects_empty_dotted_event_segments(event: str) -> None:
    with pytest.raises(ValueError):
        ingress_subject("w1", "vercel", event)


@pytest.mark.parametrize(
    "event_type",
    ["", "created", "unknown.domain.event", "ingress.linear.x", "task.cre ated"],
)
def test_event_subject_rejects_invalid_event_types(event_type: str) -> None:
    with pytest.raises(ValueError):
        event_subject("ws-1", event_type)


def test_event_subject_rejects_wildcard_workspace() -> None:
    with pytest.raises(ValueError):
        event_subject("*", "task.created")
    with pytest.raises(ValueError):
        event_subject("ws.1", "task.created")
