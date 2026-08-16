from nats.js.api import RetentionPolicy, StorageType

from jhin_events.streams import (
    AUDIT_STREAM,
    DLQ_STREAM,
    DUPLICATE_WINDOW_SECONDS,
    EVENTS_STREAM,
    INGRESS_STREAM,
    stream_configs,
)


def test_all_core_streams_defined_with_explicit_retention() -> None:
    configs = {config.name: config for config in stream_configs()}
    assert set(configs) == {INGRESS_STREAM, EVENTS_STREAM, AUDIT_STREAM, DLQ_STREAM}
    for config in configs.values():
        assert config.retention == RetentionPolicy.LIMITS
        assert config.storage == StorageType.FILE
        assert config.max_age is not None and config.max_age > 0
        assert config.duplicate_window == DUPLICATE_WINDOW_SECONDS


def test_events_stream_does_not_overlap_ingress_or_audit() -> None:
    configs = {config.name: config for config in stream_configs()}
    events_subjects = configs[EVENTS_STREAM].subjects or []
    assert all(".ingress." not in s and ".audit." not in s for s in events_subjects)
    assert "jhin.v1.*.task.>" in events_subjects
    assert "jhin.v1.*.agent.>" in events_subjects
