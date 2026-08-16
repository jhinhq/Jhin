from uuid import UUID

from jhin_events.envelope import EventEnvelope, EventSource, new_uuid7


def test_envelope_roundtrip() -> None:
    envelope = EventEnvelope(
        event_type="task.created",
        workspace_id="ws-123",
        source=EventSource(type="system"),
        data={"title": "hello"},
    )
    restored = EventEnvelope.from_bytes(envelope.to_bytes())
    assert restored == envelope
    assert restored.event_version == 1
    assert restored.data == {"title": "hello"}


def test_envelope_defaults_are_unique() -> None:
    source = EventSource(type="system")
    a = EventEnvelope(event_type="task.created", workspace_id="ws", source=source)
    b = EventEnvelope(event_type="task.created", workspace_id="ws", source=source)
    assert a.event_id != b.event_id
    assert a.correlation_id != b.correlation_id
    assert a.causation_id is None


def test_uuid7_is_time_ordered_stdlib_uuid() -> None:
    first, second = new_uuid7(), new_uuid7()
    assert isinstance(first, UUID)
    assert first.version == 7
    assert first.int < second.int
