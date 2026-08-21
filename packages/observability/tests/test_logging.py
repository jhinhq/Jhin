"""Closed JSON-v1 logging, redaction, and event-schema regressions."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace

import pytest
import structlog

import jhin_observability
from jhin_observability import (
    EVENT_FIELD_RULES,
    SafeErrorCode,
    configure_json_logging,
    filter_log_event,
    get_logger,
    normalize_connector_type,
    normalize_environment,
    normalize_event_family,
    normalize_sandbox_outcome,
    structural_redaction,
)
from jhin_observability.events import CONTEXT_FIELD_RULES
from jhin_secrets.redaction import get_redactor, redact_event_dict


class _SecretRepr:
    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


class _LeakingKeyName:
    def __init__(self, value: str) -> None:
        self._value = value
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        return self._value

    def __repr__(self) -> str:
        self.repr_calls += 1
        return self._value


class _RaisingKeyName:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        raise RuntimeError("hostile-str-canary")

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise RuntimeError("hostile-repr-canary")


class _RaisingClassKeyName:
    def __init__(self) -> None:
        self.class_reads = 0

    @property
    def __class__(self) -> type[object]:
        self.class_reads += 1
        raise RuntimeError("hostile-class-canary")


class _RaisingStripKeyName(str):
    def __new__(cls, value: str) -> _RaisingStripKeyName:
        instance = super().__new__(cls, value)
        instance.strip_calls = 0
        return instance

    def strip(self, chars: str | None = None, /) -> str:
        self.strip_calls += 1
        raise RuntimeError("hostile-strip-canary")


@pytest.fixture(autouse=True)
def clear_process_secret_registry() -> Iterator[None]:
    redactor = get_redactor()
    redactor.clear()
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_named = {
        candidate: (list(candidate.handlers), candidate.level, candidate.propagate)
        for candidate in logging.root.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    }
    original_structlog_config = dict(structlog.get_config())
    try:
        yield
    finally:
        redactor.clear()
        installed_handlers = [
            handler for handler in root.handlers if handler not in original_handlers
        ]
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for named, (handlers, level, propagate) in original_named.items():
            named.handlers[:] = handlers
            named.setLevel(level)
            named.propagate = propagate
        for handler in installed_handlers:
            handler.close()
        structlog.configure(**original_structlog_config)


@pytest.mark.parametrize("logger_kind", ["structlog", "stdlib"])
def test_every_record_has_exact_v1_required_fields(
    capsys: pytest.CaptureFixture[str], logger_kind: str
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    if logger_kind == "structlog":
        get_logger("jhin.test").info("api.started", request_id="req-1")
    else:
        logging.getLogger("uvicorn.error").warning("server booted on private-host-canary")
    record = json.loads(capsys.readouterr().out)
    assert record["schema_version"] == 1
    assert record["service"] == "api"
    assert record["environment"] == "test"
    assert record["level"] in {"info", "warning"}
    assert record["event"] in {"api.started", "stdlib.message"}
    assert record["logger"] in {"jhin.test", "uvicorn.error"}
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo
    assert "private-host-canary" not in json.dumps(record)


def test_retained_structlog_proxy_uses_latest_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    retained_logger = get_logger("jhin.reconfiguration")
    configure_json_logging(service="api", environment="staging", level="INFO")
    retained_logger.info("api.started", request_id="req-first")
    first = json.loads(capsys.readouterr().out)
    assert first["service"] == "api"
    assert first["environment"] == "staging"

    configure_json_logging(
        service="rootless-docker-transport",
        environment="test",
        level="INFO",
    )
    retained_logger.info("rootless_transport.ready")
    second_rendered = capsys.readouterr().out
    second = json.loads(second_rendered)
    assert second["schema_version"] == 1
    assert second["service"] == "rootless-docker-transport"
    assert second["environment"] == "test"
    assert second["event"] == "rootless_transport.ready"
    assert "api" not in second_rendered
    assert "staging" not in second_rendered


def test_preexisting_named_handler_is_forced_through_single_json_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "named-handler-message-canary"
    named = logging.getLogger("uvicorn.error")
    original_handlers = list(named.handlers)
    original_level = named.level
    original_propagate = named.propagate
    raw_handler = logging.StreamHandler()
    raw_handler.setFormatter(logging.Formatter("RAW:%(message)s"))
    named.handlers[:] = [raw_handler]
    named.setLevel(logging.INFO)
    named.propagate = False
    try:
        configure_json_logging(service="api", environment="test", level="INFO")
        named.warning("server booted with %s", canary)

        captured = capsys.readouterr()
        record = json.loads(captured.out)
        assert record["event"] == "stdlib.message"
        assert record["logger"] == "uvicorn.error"
        assert captured.err == ""
        assert canary not in captured.out
        assert "server booted" not in captured.out
    finally:
        named.handlers[:] = original_handlers
        named.setLevel(original_level)
        named.propagate = original_propagate
        raw_handler.close()


def test_structural_redaction_removes_nested_keys_and_url_parts() -> None:
    value = {
        "authorization": "Bearer exact-canary",
        "nested": {"api_key": "key-canary", "safe": "kept"},
        "target": "https://user:pass@example.test/path?token=query-canary#fragment-canary",
    }
    redacted = structural_redaction(value)
    assert isinstance(redacted, dict)
    rendered = json.dumps(redacted)
    assert "exact-canary" not in rendered
    assert "key-canary" not in rendered
    assert "user" not in rendered and "pass" not in rendered
    assert "query-canary" not in rendered and "fragment-canary" not in rendered
    assert isinstance(redacted["nested"], dict)
    assert redacted["nested"]["safe"] == "kept"
    assert redacted["target"] == "https://example.test/path"


def test_sensitive_key_name_is_one_public_authority() -> None:
    from jhin_observability import is_sensitive_key_name
    from jhin_observability.redaction import (
        is_sensitive_key_name as redaction_predicate,
    )

    assert "is_sensitive_key_name" in jhin_observability.__all__
    assert is_sensitive_key_name is redaction_predicate


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "Authorization",
        "http_authorization",
        "httpAuthorization",
        "http.authorization",
        "cookie",
        "Cookie",
        "set_cookie",
        "setCookie",
        "set-cookie",
        "password",
        "Password",
        "database_password",
        "databasePassword",
        "database/password",
        "secret",
        "Secret",
        "client_secret",
        "clientSecret",
        "client.secret",
        "token",
        "Token",
        "access_token",
        "accessToken",
        "access-token",
        "api_key",
        "apiKey",
        "service_api_key",
        "serviceApiKey",
        "service-api-key",
        "private_key",
        "privateKey",
        "signing_private_key",
        "signingPrivateKey",
        "signing.private-key",
        "dsn",
        "Dsn",
        "database_dsn",
        "databaseDsn",
        "database-dsn",
    ],
)
def test_sensitive_key_name_recognizes_existing_families_and_suffixes(key: str) -> None:
    from jhin_observability import is_sensitive_key_name

    assert is_sensitive_key_name(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "completion",
        "sql",
        "tool_input",
        "tool_output",
        "request_body",
        "response_body",
        "webhook_payload",
        "secret_env",
    ],
)
def test_sensitive_key_name_preserves_exact_payload_field_authority(key: str) -> None:
    from jhin_observability import is_sensitive_key_name

    assert is_sensitive_key_name(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "secretary",
        "authorization_url",
        "cookie_count",
        "password_reset",
        "token_count",
        "api_keys",
        "public_key",
        "private_key_id",
        "dsn_label",
        "",
        "  ",
    ],
)
def test_sensitive_key_name_does_not_widen_benign_names(key: str) -> None:
    from jhin_observability import is_sensitive_key_name

    assert is_sensitive_key_name(key) is False


@pytest.mark.parametrize(
    "value",
    [None, True, 42, 3.14, b"secret", ["token"], {"api_key": "value"}],
)
def test_sensitive_key_name_rejects_non_strings_without_coercion(value: object) -> None:
    from jhin_observability import is_sensitive_key_name

    assert is_sensitive_key_name(value) is False


def test_sensitive_key_name_does_not_inspect_or_echo_hostile_objects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jhin_observability import is_sensitive_key_name

    leaking = _LeakingKeyName("api_key")
    raising = _RaisingKeyName()

    assert is_sensitive_key_name(leaking) is False
    assert is_sensitive_key_name(raising) is False
    assert leaking.str_calls == 0
    assert leaking.repr_calls == 0
    assert raising.str_calls == 0
    assert raising.repr_calls == 0
    captured = capsys.readouterr()
    assert "api_key" not in captured.out
    assert "api_key" not in captured.err
    assert "hostile" not in captured.out
    assert "hostile" not in captured.err


def test_sensitive_key_name_does_not_read_hostile_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jhin_observability import is_sensitive_key_name

    value = _RaisingClassKeyName()

    assert is_sensitive_key_name(value) is False
    assert value.class_reads == 0
    captured = capsys.readouterr()
    assert "hostile-class-canary" not in captured.out
    assert "hostile-class-canary" not in captured.err


def test_sensitive_key_name_rejects_str_subclass_without_calling_strip(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jhin_observability import is_sensitive_key_name

    value = _RaisingStripKeyName("api_key")

    assert is_sensitive_key_name(value) is False
    assert value.strip_calls == 0
    captured = capsys.readouterr()
    assert "hostile-strip-canary" not in captured.out
    assert "hostile-strip-canary" not in captured.err


def test_structural_redaction_routes_keys_through_public_sensitive_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_observability.redaction as redaction_module

    monkeypatch.setattr(
        redaction_module,
        "is_sensitive_key_name",
        lambda value: value == "delegated_sensitive_field",
    )

    assert structural_redaction({"delegated_sensitive_field": "canary", "safe": "kept"}) == {
        "delegated_sensitive_field": "[REDACTED]",
        "safe": "kept",
    }


def test_unknown_object_is_stringified_only_inside_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "unknown-object-canary"
    get_redactor().register(canary)
    configure_json_logging(
        service="tool-worker",
        environment="test",
        level="INFO",
        extra_processors=(redact_event_dict,),
    )
    get_logger(__name__).info("api.started", request_id=_SecretRepr(canary))
    rendered = capsys.readouterr().out
    assert json.loads(rendered)["event"] == "api.started"
    assert canary not in rendered


def test_exception_becomes_bounded_redacted_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_redactor().register("trace-canary")
    configure_json_logging(
        service="api",
        environment="test",
        level="INFO",
        extra_processors=(redact_event_dict,),
    )
    try:
        raise RuntimeError("request failed with password=trace-canary")
    except RuntimeError:
        get_logger(__name__).exception(
            "api.request_failed",
            error_code=SafeErrorCode.INTERNAL_ERROR.value,
        )
    record = json.loads(capsys.readouterr().out)
    assert record["error"]["type"] == "RuntimeError"
    assert record["error"]["code"] == "internal_error"
    assert len(record["error"]["traceback"]) <= 32
    assert "trace-canary" not in json.dumps(record)


@pytest.mark.parametrize(
    "key",
    ["apiKey", "privateKey", "accessToken", "clientSecret", "Authorization", "set-cookie"],
)
def test_credential_key_normalization_redacts_camel_case_and_hyphenated_keys(key: str) -> None:
    assert structural_redaction({key: "credential-canary"}) == {key: "[REDACTED]"}


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "completion",
        "sql",
        "tool_input",
        "tool_output",
        "request_body",
        "response_body",
        "webhook_payload",
        "secret_env",
    ],
)
def test_payload_fields_are_always_redacted(key: str) -> None:
    assert structural_redaction({key: "payload-canary"}) == {key: "[REDACTED]"}


def test_redaction_bounds_are_exact() -> None:
    nested: object = "leaf"
    for _ in range(9):
        nested = {"child": nested}
    redacted = structural_redaction(
        {
            "nested": nested,
            "mapping": {str(i): i for i in range(65)},
            "items": list(range(65)),
            "text": "x" * 2_001,
        }
    )
    assert isinstance(redacted, dict)
    assert "[TRUNCATED]" in json.dumps(redacted)
    assert isinstance(redacted["mapping"], dict)
    assert isinstance(redacted["items"], list)
    assert isinstance(redacted["text"], str)
    assert len(redacted["mapping"]) == 64
    assert len(redacted["items"]) == 64
    assert len(redacted["text"]) == 2_000


def test_event_filter_discards_unregistered_fields_and_foreign_text() -> None:
    filtered = filter_log_event(
        {
            "event": "worker.started",
            "task_queue": "jhin-agent-queue",
            "message": "foreign-free-text-canary",
            "detail": "foreign-detail-canary",
        }
    )
    assert filtered == {"event": "worker.started", "task_queue": "jhin-agent-queue"}


def test_unknown_event_is_replaced_without_preserving_original_text() -> None:
    filtered = filter_log_event({"event": "attacker supplied free text", "safe": "canary"})
    assert filtered == {"event": "log.event_rejected"}


@pytest.mark.parametrize("event", sorted(EVENT_FIELD_RULES))
def test_every_registered_event_rejects_an_unknown_canary_field(event: str) -> None:
    filtered = filter_log_event({"event": event, "unregistered": "runtime-canary"})
    assert "runtime-canary" not in json.dumps(filtered)


@pytest.mark.parametrize("event", sorted(EVENT_FIELD_RULES))
def test_every_registered_event_survives_the_runtime_renderer(
    event: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    get_logger("jhin.contract").info(event)
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == event
    assert {
        "schema_version",
        "timestamp",
        "level",
        "service",
        "environment",
        "logger",
    } <= record.keys()


def test_job_id_is_allowed_only_on_sandbox_job_finished() -> None:
    valid = filter_log_event(
        {
            "event": "sandbox.job.finished",
            "job_id": "0123456789abcdef",
            "outcome": "completed",
        }
    )
    foreign = filter_log_event({"event": "worker.started", "job_id": "0123456789abcdef"})
    assert valid["job_id"] == "0123456789abcdef"
    assert "job_id" not in foreign
    assert "job_id" not in CONTEXT_FIELD_RULES


@pytest.mark.parametrize("accepted", ["export_timeout", "export_failed"])
def test_export_failure_codes_are_event_and_field_specific(accepted: str) -> None:
    assert (
        filter_log_event({"event": "telemetry.export_failed", "error_code": accepted})["error_code"]
        == accepted
    )


@pytest.mark.parametrize("rejected", ["internal_error", "timeout", "attacker-code"])
def test_export_failure_rejects_non_export_error_codes(rejected: str) -> None:
    assert "error_code" not in filter_log_event(
        {"event": "telemetry.export_failed", "error_code": rejected}
    )


def test_export_failure_accepts_no_structured_error_or_foreign_fields() -> None:
    assert filter_log_event(
        {
            "event": "telemetry.export_failed",
            "error_code": "export_failed",
            "error": {"type": "RuntimeError", "code": "internal_error"},
            "endpoint": "https://collector-user:collector-pass@example.test",
        }
    ) == {"event": "telemetry.export_failed", "error_code": "export_failed"}


def test_structured_error_is_allowed_only_by_its_event_registry() -> None:
    structured = {"type": "RuntimeError", "code": "internal_error", "traceback": []}
    assert (
        filter_log_event({"event": "api.request_failed", "error": structured})["error"]["type"]
        == "RuntimeError"
    )
    assert "error" not in filter_log_event({"event": "worker.started", "error": structured})


def test_health_event_reservation_has_no_event_fields() -> None:
    event = "health.heartbeat_write_failed"
    assert EVENT_FIELD_RULES[event] == {}
    assert filter_log_event({"event": event, "reason": "canary"}) == {"event": event}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("dev", "dev"),
        (" TEST ", "test"),
        ("Staging", "staging"),
        ("production", "production"),
        ("development", "dev"),
        ("prod", "production"),
        (SimpleNamespace(value="TEST"), "test"),
        ("unknown", "production"),
        (None, "production"),
    ],
)
def test_environment_normalizer_is_closed(raw: object, expected: str) -> None:
    assert normalize_environment(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github", "github"),
        (" LINEAR ", "linear"),
        ("vercel", "vercel"),
        ("supabase", "supabase"),
        ("cli", "cli"),
        (SimpleNamespace(value="GITHUB"), "github"),
        ("unknown", "other"),
        (None, "other"),
    ],
)
def test_connector_type_normalizer_is_closed(raw: object, expected: str) -> None:
    assert normalize_connector_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("connector.github", "connector"),
        (" TASK.STARTED ", "task"),
        ("run", "run"),
        ("tool.completed", "tool"),
        ("approval", "approval"),
        (SimpleNamespace(value="RUN.FINISHED"), "run"),
        ("unknown.event", "other"),
        (None, "other"),
    ],
)
def test_event_family_normalizer_is_closed(raw: object, expected: str) -> None:
    assert normalize_event_family(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ok", "ok"),
        (" ACCEPTED ", "accepted"),
        ("running", "started"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("timeout", "timeout"),
        ("duplicate", "duplicate"),
        (SimpleNamespace(value="RUNNING"), "started"),
        ("unknown", "other"),
        (None, "other"),
    ],
)
def test_sandbox_outcome_normalizer_is_closed(raw: object, expected: str) -> None:
    assert normalize_sandbox_outcome(raw) == expected
