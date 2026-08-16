"""Redaction unit tests (plan 32.5: redactor strips known secret)."""

from jhin_secrets.redaction import REDACTED, SecretRedactor, get_redactor, redact_event_dict


def test_redactor_strips_known_secret() -> None:
    redactor = SecretRedactor()
    redactor.register("sk-live-abc123def456")
    result = redactor.redact_text("failed with key sk-live-abc123def456 (401)")
    assert "sk-live-abc123def456" not in result
    assert REDACTED in result


def test_redactor_handles_multiple_occurrences_and_values() -> None:
    redactor = SecretRedactor()
    redactor.register("secret-one-value")
    redactor.register("secret-two-value")
    text = "a=secret-one-value b=secret-two-value c=secret-one-value"
    result = redactor.redact_text(text)
    assert "secret-one-value" not in result
    assert "secret-two-value" not in result
    assert result.count(REDACTED) == 3


def test_short_values_are_not_registered() -> None:
    redactor = SecretRedactor()
    redactor.register("ab")
    assert redactor.redact_text("ab is fine") == "ab is fine"


def test_redact_value_recurses_containers() -> None:
    redactor = SecretRedactor()
    redactor.register("tok-secret-value")
    nested = {"error": "auth tok-secret-value failed", "items": ["ok", "tok-secret-value"]}
    result = redactor.redact_value(nested)
    assert result["error"] == f"auth {REDACTED} failed"
    assert result["items"][1] == REDACTED


def test_structlog_processor_scrubs_event_dict() -> None:
    get_redactor().register("proc-secret-value")
    try:
        event = redact_event_dict(None, "info", {"event": "call failed: proc-secret-value"})
        assert event["event"] == f"call failed: {REDACTED}"
    finally:
        get_redactor().clear()
