"""Unit tests for audit emission: record() stages append-only INSERTs."""

from typing import Any

from jhin_api.audit import service as audit
from jhin_db.models import AuditEvent
from jhin_domain import ActorType, new_uuid7


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_record_stages_an_audit_event_with_all_fields() -> None:
    session = FakeSession()
    workspace_id, actor_id, target_id, request_id = (new_uuid7() for _ in range(4))

    event = audit.record(
        session,  # type: ignore[arg-type]  (duck-typed: only .add is used)
        action="agent.created",
        target_type="agent",
        target_id=target_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        request_id=request_id,
        ip_hash="abc123",
        metadata={"name": "CTO"},
    )

    assert session.added == [event]
    assert isinstance(event, AuditEvent)
    assert event.workspace_id == workspace_id
    assert event.actor_type == ActorType.USER.value
    assert event.actor_id == actor_id
    assert event.action == "agent.created"
    assert event.target_type == "agent"
    assert event.target_id == target_id
    assert event.request_id == request_id
    assert event.ip_hash == "abc123"
    assert event.metadata_json == {"name": "CTO"}


def test_record_defaults_are_safe() -> None:
    session = FakeSession()
    event = audit.record(
        session,  # type: ignore[arg-type]
        action="auth.login_failed",
        target_type="user",
        workspace_id=None,
        actor_type=ActorType.SYSTEM,
    )
    assert event.metadata_json == {}
    assert event.actor_id is None
    assert event.workspace_id is None
    assert event.actor_type == "system"


def test_audit_module_has_no_update_or_delete_paths() -> None:
    """Append-only guard (plan 23): the service must never grow mutators."""
    exported = {name for name in dir(audit) if not name.startswith("_")}
    forbidden = {name for name in exported if "update" in name or "delete" in name}
    assert not forbidden, f"audit service must stay append-only, found: {forbidden}"
