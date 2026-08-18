"""Credential-safe structured logging regressions."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from jhin_observability import configure_logging, get_logger
from jhin_secrets.redaction import REDACTED, get_redactor, redact_event_dict


@pytest.fixture(autouse=True)
def _restore_global_logging_state() -> None:
    """Do not leave capsys-backed handlers installed for later test teardown."""

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_structlog_config = dict(structlog.get_config())
    try:
        yield
    finally:
        installed_handlers = [
            handler for handler in root.handlers if handler not in original_handlers
        ]
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for handler in installed_handlers:
            handler.close()
        structlog.configure(**original_structlog_config)


class _SecretRepr:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __repr__(self) -> str:
        return f"provider object containing {self._secret}"


class _SecretStructlog:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __structlog__(self) -> str:
        return f"provider object containing {self._secret}"


@pytest.mark.parametrize("logger_kind", ["structlog", "stdlib"])
def test_traceback_is_redacted_after_structured_exception_rendering(
    capsys: pytest.CaptureFixture[str],
    logger_kind: str,
) -> None:
    secret = "traceback-provider-secret-value"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    configure_logging("traceback-test", extra_processors=[redact_event_dict])

    try:
        try:
            raise RuntimeError(f"provider failed with credential {secret}")
        except RuntimeError:
            if logger_kind == "structlog":
                get_logger("traceback-test").exception("provider.call_failed")
            else:
                logging.getLogger("traceback-test").exception("provider.call_failed")

        record = json.loads(capsys.readouterr().out)
        rendered = json.dumps(record, ensure_ascii=False)
        assert secret not in rendered
        assert REDACTED in rendered
    finally:
        redactor.clear()


@pytest.mark.parametrize("value_factory", [_SecretRepr, _SecretStructlog])
def test_custom_log_objects_cannot_render_secrets_after_redaction(
    capsys: pytest.CaptureFixture[str],
    value_factory: type[_SecretRepr] | type[_SecretStructlog],
) -> None:
    secret = "object-provider-secret-value"
    redactor = get_redactor()
    redactor.clear()
    redactor.register(secret)
    configure_logging("object-test", extra_processors=[redact_event_dict])

    try:
        get_logger("object-test").info("provider.response", provider=value_factory(secret))

        rendered = capsys.readouterr().out
        assert secret not in rendered
    finally:
        redactor.clear()
