"""Closed JSON-v1 logging, redaction, and event-schema regressions."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import create_async_engine

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
from jhin_observability.events import CONTEXT_FIELD_RULES, MAX_LIBRARY_MESSAGE_CHARS
from jhin_observability.redaction import LOG_SCHEMA_VERSION
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


_hostile_class_reads = [0]
_hostile_strip_calls = [0]


class _RaisingClassKeyName:
    def __getattribute__(self, name: str) -> object:
        if name == "__class__":
            _hostile_class_reads[0] += 1
            raise RuntimeError("hostile-class-canary")
        return super().__getattribute__(name)


class _RaisingStripKeyName(str):
    def strip(self, chars: str | None = None, /) -> str:
        _hostile_strip_calls[0] += 1
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
def test_every_record_has_exact_required_contract_fields(
    capsys: pytest.CaptureFixture[str], logger_kind: str
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    if logger_kind == "structlog":
        get_logger("jhin.test").info("api.started", request_id="req-1")
    else:
        logging.getLogger("uvicorn.error").warning("server booted on private-host-canary")
    record = json.loads(capsys.readouterr().out)
    assert record["schema_version"] == LOG_SCHEMA_VERSION
    assert record["service"] == "api"
    assert record["environment"] == "test"
    assert record["level"] in {"info", "warning"}
    assert record["event"] in {"api.started", "stdlib.message"}
    assert record["logger"] in {"jhin.test", "uvicorn.error"}
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).tzinfo
    # A structlog event is a registered name and carries no free text; a
    # foreign record keeps the sentence its library wrote, which is the only
    # place that sentence exists.
    if logger_kind == "structlog":
        assert "message" not in record
    else:
        assert record["message"] == "server booted on private-host-canary"


def test_library_message_is_kept_but_bounded_stripped_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    redactor = get_redactor()
    redactor.register("token-canary")
    configure_json_logging(
        service="tool-worker",
        environment="test",
        level="INFO",
        extra_processors=(redact_event_dict,),
    )
    logging.getLogger("httpx").warning(
        "HTTP Request:\x1b[31m GET https://user:pw@example.test/p?key=query-canary "
        "with token-canary " + ("x" * 4_000)
    )
    record = json.loads(capsys.readouterr().out)

    message = record["message"]
    assert record["event"] == "stdlib.message"
    assert len(message) <= MAX_LIBRARY_MESSAGE_CHARS
    assert "\x1b" not in message and "\n" not in message
    assert "query-canary" not in message
    assert "pw@" not in message
    assert "token-canary" not in message
    assert "[REDACTED]" in message


def test_library_exception_reaches_the_line_as_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_json_logging(service="agent-worker", environment="test", level="INFO")
    try:
        raise RuntimeError("provider-detail-canary")
    except RuntimeError:
        logging.getLogger("temporalio.activity").warning(
            "Completing activity as failed", exc_info=True
        )
    rendered = capsys.readouterr().out
    record = json.loads(rendered)

    assert record["event"] == "stdlib.message"
    assert record["message"] == "Completing activity as failed"
    assert record["error"]["type"] == "RuntimeError"
    assert record["error"]["code"] == SafeErrorCode.INTERNAL_ERROR.value
    assert record["error"]["traceback"][-1]["function"] == (
        "test_library_exception_reaches_the_line_as_a_structured_error"
    )
    assert "provider-detail-canary" not in rendered


def test_library_message_that_is_only_control_characters_is_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_json_logging(service="api", environment="test", level="INFO")
    logging.getLogger("uvicorn.error").warning("\x00\x1b\n\t")
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "stdlib.message"
    assert "message" not in record


@pytest.mark.asyncio
async def test_a_bound_parameter_cannot_reach_a_log_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole finding, run through the real engine and the real config.

    ``echo=False`` is not what gates SQLAlchemy's statement log — the logger's
    effective level is — so a service at INFO used to write its own SQL and
    every bound parameter to stdout. Both layers are asserted: the records are
    not emitted at all, and (below) the text would not survive even if they
    were.
    """
    configure_json_logging(service="tool-worker", environment="test", level="INFO")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE t (a INTEGER, b TEXT)"))
            await connection.execute(
                sa.text("INSERT INTO t VALUES (:a, :b)"),
                {"a": 1, "b": "ghu_unregistered_token_abc123"},
            )
    finally:
        await engine.dispose()

    rendered = capsys.readouterr().out
    assert "ghu_unregistered_token_abc123" not in rendered
    assert "INSERT INTO" not in rendered
    assert logging.getLogger("sqlalchemy.engine.Engine").isEnabledFor(logging.INFO) is False


def test_a_data_carrying_logger_keeps_its_name_and_loses_its_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The second layer, with the first one deliberately defeated.

    A ``sqlalchemy.engine`` record at WARNING is emitted whatever the level
    pin says, and a future engine configured with ``echo=True`` would emit at
    INFO as well. Neither can put a statement or a parameter on a line: the
    text allow-list is what decides, and no ``sqlalchemy`` logger is on it.
    """
    configure_json_logging(service="tool-worker", environment="test", level="INFO")
    logging.getLogger("sqlalchemy.engine.Engine").warning(
        "[generated in 0.00015s] (1, 'ghu_unregistered_token_abc123')"
    )
    rendered = capsys.readouterr().out
    record = json.loads(rendered)

    assert "ghu_unregistered_token_abc123" not in rendered
    assert record["event"] == "stdlib.message"
    assert record["logger"] == "sqlalchemy.engine.Engine"
    assert record["level"] == "warning"
    assert "message" not in record


def test_an_unread_library_is_silent_rather_than_leaking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A logger nobody has vetted is not a logger whose words are kept.

    This is the direction the allow-list is chosen for: adding a dependency
    cannot open a new text channel by itself.
    """
    configure_json_logging(service="api", environment="test", level="INFO")
    logging.getLogger("some_new_dependency.client").warning("payload=customer-canary")
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "stdlib.message"
    assert record["logger"] == "some_new_dependency.client"
    assert "message" not in record
    assert "customer-canary" not in json.dumps(record)


def test_the_access_log_query_string_is_not_a_text_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``uvicorn`` is allow-listed; ``uvicorn.access`` is denied under it.

    An access line is a bare path, not a URL, so the URL sanitizer never sees
    it — and ``/oauth/callback?code=...`` is an access log entry and an
    authorization code at the same time.
    """
    configure_json_logging(service="api", environment="test", level="INFO")
    logging.getLogger("uvicorn.access").info(
        '127.0.0.1:52000 - "GET /oauth/callback?code=code-canary HTTP/1.1" 302'
    )
    record = json.loads(capsys.readouterr().out)
    assert record["logger"] == "uvicorn.access"
    assert "message" not in record
    assert "code-canary" not in json.dumps(record)

    logging.getLogger("uvicorn.error").info("Application startup complete.")
    assert json.loads(capsys.readouterr().out)["message"] == "Application startup complete."


def test_asyncios_unretrieved_exception_cannot_carry_its_own_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``asyncio``'s default exception handler formats ``repr(future)``.

    That repr contains the exception's own ``str``, so allowing this one
    library's text re-admitted exactly the free exception text
    ``_normalize_exception`` strips from every other record — a DSN password
    and a token-shaped string both reached a line this way.
    """
    configure_json_logging(service="agent-worker", environment="test", level="INFO")
    logging.getLogger("asyncio").error(
        "Future exception was never retrieved\nfuture: <Future finished "
        "exception=OperationalError('connection to "
        "postgresql://jhin:pw-canary@postgres:5432/jhin failed; token ghu_tok-canary')>"
    )
    rendered = capsys.readouterr().out
    record = json.loads(rendered)

    assert record["event"] == "stdlib.message"
    assert record["logger"] == "asyncio"
    assert record["level"] == "error"
    assert "message" not in record
    assert "pw-canary" not in rendered
    assert "tok-canary" not in rendered


def test_a_trace_records_response_headers_are_not_a_text_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``httpcore`` writes response headers into its trace records.

    Including ``set-cookie``. They are DEBUG, which is the only reason nobody
    had seen one: bootstrap passed no level and every service sat at INFO.
    Now that ``LOG_LEVEL`` actually reaches the root logger, an install at
    DEBUG would have written them.
    """
    configure_json_logging(service="api", environment="test", level="DEBUG")
    logging.getLogger("httpcore.http11").debug(
        "receive_response_headers.complete return_value=(b'HTTP/1.1', 200, "
        "[(b'set-cookie', b'session=cookie-canary; HttpOnly')])"
    )
    rendered = capsys.readouterr().out
    record = json.loads(rendered)

    assert record["event"] == "stdlib.message"
    assert record["logger"] == "httpcore.http11"
    assert "message" not in record
    assert "cookie-canary" not in rendered

    # httpx, one layer up, still says what it did.
    logging.getLogger("httpx").info('HTTP Request: GET https://example.test/p "200 OK"')
    assert "HTTP Request" in json.loads(capsys.readouterr().out)["message"]


def test_a_non_http_connection_string_loses_its_password(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sanitizer matched ``https?://`` and nothing else.

    ``DATABASE_URL`` and ``NATS_URL`` are exactly the shapes it did not match,
    and neither is registered with the process redactor: a connection string is
    configuration rather than a credential anybody declared.
    """
    configure_json_logging(service="tool-worker", environment="test", level="INFO")
    logging.getLogger("temporalio.client").warning(
        "connect failed: postgresql+asyncpg://jhin:dsn-canary@postgres:5432/jhin?sslmode=q-canary "
        "and nats://user:nats-canary@nats:4222 and amqp://u:amqp-canary@broker:5672/v"
    )
    rendered = capsys.readouterr().out
    record = json.loads(rendered)

    assert "dsn-canary" not in rendered
    assert "q-canary" not in rendered
    assert "nats-canary" not in rendered
    assert "amqp-canary" not in rendered
    # What the sentence was about survives: scheme, host, port and path.
    assert "postgresql+asyncpg://postgres:5432/jhin" in record["message"]
    assert "nats://nats:4222" in record["message"]


def test_a_connection_string_in_a_field_loses_its_password() -> None:
    """The same hole through the structural pass rather than the text one."""
    redacted = structural_redaction(
        {"upstream": "redis://default:redis-canary@cache:6379/0?auth=also-canary"}
    )
    assert isinstance(redacted, dict)
    assert redacted["upstream"] == "redis://cache:6379/0"


def test_the_configured_log_level_reaches_the_root_logger() -> None:
    """``ObservabilitySettings.log_level`` read ``LOG_LEVEL`` and went nowhere.

    The config had no field for it and bootstrap never passed one, so every
    service ran at the default whatever its compose file said — and an
    operator who set DEBUG to debug something got INFO and no error.
    """
    from jhin_observability.config import ObservabilityConfig, ObservabilitySettings

    settings = ObservabilitySettings(app_env="test", log_level="debug")
    config = settings.observability_config(service_name="api", service_version="0.0.0")
    assert config.log_level == "DEBUG"

    with pytest.raises(ValueError, match="log level must be one of"):
        ObservabilityConfig(
            service_name="api",
            service_version="0.0.0",
            environment="test",
            log_level="chatty",
        )


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
    assert second["schema_version"] == LOG_SCHEMA_VERSION
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
        # The pre-existing raw formatter is gone: the text appears exactly
        # once, inside the single JSON line, and never in that handler's
        # own shape.
        assert record["message"] == f"server booted with {canary}"
        assert "RAW:" not in captured.out
        assert captured.out.count(canary) == 1
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


def test_sensitive_key_name_recognizes_existing_families_and_suffixes() -> None:
    from jhin_observability import is_sensitive_key_name

    for key in [
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
    ]:
        assert is_sensitive_key_name(key) is True, f"key={key!r}"


def test_sensitive_key_name_recognizes_uppercase_and_acronym_families() -> None:
    from jhin_observability import is_sensitive_key_name

    for key in [
        "AUTHORIZATION",
        "COOKIE",
        "PASSWORD",
        "SECRET",
        "TOKEN",
        "API_KEY",
        "X-API-KEY",
        "PRIVATE_KEY",
        "DSN",
        "X-DSN",
        "APIKey",
        "httpAPIKey",
        "HttpAPIKey",
        "HTTPAuthorization",
        "signingPRIVATEKey",
        "databaseDSN",
        "xDSN",
    ]:
        assert is_sensitive_key_name(key) is True, f"key={key!r}"


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


def test_sensitive_key_name_does_not_widen_benign_names() -> None:
    from jhin_observability import is_sensitive_key_name

    for key in [
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
    ]:
        assert is_sensitive_key_name(key) is False, f"key={key!r}"


@pytest.mark.parametrize(
    "key",
    [
        "SECRETARY",
        "AUTHORIZATION_URL",
        "COOKIE_COUNT",
        "PASSWORD_RESET",
        "TOKEN_COUNT",
        "API_KEYS",
        "PUBLIC_KEY",
        "PRIVATE_KEY_ID",
        "DSN_LABEL",
        "SAFE_API_KEY_LABEL",
    ],
)
def test_sensitive_key_name_does_not_widen_uppercase_near_misses(key: str) -> None:
    from jhin_observability import is_sensitive_key_name

    assert is_sensitive_key_name(key) is False


def test_sensitive_key_name_bounds_large_uppercase_normalization_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jhin_observability.redaction as redaction_module
    from jhin_observability import is_sensitive_key_name

    def reject_regex_work(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("large-key normalization delegated to regex")

    monkeypatch.setattr(
        redaction_module,
        "re",
        SimpleNamespace(sub=reject_regex_work),
        raising=False,
    )

    assert is_sensitive_key_name("A" * 100_000) is False


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

    _hostile_class_reads[0] = 0
    value = _RaisingClassKeyName()

    assert is_sensitive_key_name(value) is False
    assert _hostile_class_reads[0] == 0
    with pytest.raises(RuntimeError, match="hostile-class-canary"):
        isinstance(value, str)
    assert _hostile_class_reads[0] == 1
    captured = capsys.readouterr()
    assert "hostile-class-canary" not in captured.out
    assert "hostile-class-canary" not in captured.err


def test_sensitive_key_name_rejects_str_subclass_without_calling_strip(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jhin_observability import is_sensitive_key_name

    _hostile_strip_calls[0] = 0
    value = _RaisingStripKeyName("api_key")

    assert is_sensitive_key_name(value) is False
    assert _hostile_strip_calls[0] == 0
    assert isinstance(value, str) is True
    with pytest.raises(RuntimeError, match="hostile-strip-canary"):
        value.strip()
    assert _hostile_strip_calls[0] == 1
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


def test_every_registered_event_rejects_an_unknown_canary_field() -> None:
    for event in sorted(EVENT_FIELD_RULES):
        filtered = filter_log_event({"event": event, "unregistered": "runtime-canary"})
        assert "runtime-canary" not in json.dumps(filtered), f"event={event!r}"


def test_every_registered_event_survives_the_runtime_renderer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for event in sorted(EVENT_FIELD_RULES):
        configure_json_logging(service="api", environment="test", level="INFO")
        get_logger("jhin.contract").info(event)
        record = json.loads(capsys.readouterr().out)
        assert record["event"] == event, f"event={event!r}"
        assert {
            "schema_version",
            "timestamp",
            "level",
            "service",
            "environment",
            "logger",
        } <= record.keys(), f"event={event!r}"


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


def test_one_task_logging_alias_is_removed_without_changing_json_entrypoint() -> None:
    assert callable(configure_json_logging)
    assert "configure_json_logging" in jhin_observability.__all__
    assert "configure_logging" not in jhin_observability.__all__
    assert not hasattr(jhin_observability, "configure_logging")
