"""Statement-free SQLAlchemy tracing tests."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event as sqlalchemy_event

import jhin_observability.sqlalchemy as sqlalchemy_observability
from jhin_observability import SafeErrorCode, noop_tracer

KNOWN_TABLES = frozenset({"secret", "workspace"})
_SYNC_ENGINE = object()


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (
            "SELECT value FROM secret WHERE token = :token",
            {"db.system": "postgresql", "db.operation": "SELECT", "db.table": "secret"},
        ),
        (
            "  insert INTO workspace (id) VALUES (:id)",
            {"db.system": "postgresql", "db.operation": "INSERT", "db.table": "workspace"},
        ),
        (
            "UPDATE secret SET value = :value",
            {"db.system": "postgresql", "db.operation": "UPDATE", "db.table": "secret"},
        ),
        (
            "DELETE FROM workspace WHERE id = :id",
            {"db.system": "postgresql", "db.operation": "DELETE", "db.table": "workspace"},
        ),
        (
            "MERGE INTO secret USING workspace ON secret.id = workspace.id",
            {"db.system": "postgresql", "db.operation": "MERGE", "db.table": "secret"},
        ),
        (
            "CREATE TABLE workspace (id INTEGER)",
            {"db.system": "postgresql", "db.operation": "CREATE", "db.table": "workspace"},
        ),
        (
            "ALTER TABLE secret ADD COLUMN value TEXT",
            {"db.system": "postgresql", "db.operation": "ALTER", "db.table": "secret"},
        ),
        (
            "DROP TABLE workspace",
            {"db.system": "postgresql", "db.operation": "DROP", "db.table": "workspace"},
        ),
        (
            "SELECT * FROM attacker_supplied",
            {"db.system": "postgresql", "db.operation": "SELECT", "db.table": "other"},
        ),
        (
            "EXEC secret_procedure",
            {"db.system": "postgresql", "db.operation": "other", "db.table": "other"},
        ),
    ],
)
def test_normalized_sql_metadata_uses_a_closed_grammar(
    statement: str, expected: dict[str, str]
) -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(statement, known_tables=KNOWN_TABLES) == expected


@pytest.mark.parametrize(
    ("statement", "operation", "expected_table"),
    [
        ("SELECT value FROM secret", "SELECT", "secret"),
        ("SELECT value FROM secret;", "SELECT", "secret"),
        ("SELECT * FROM secret()", "SELECT", "other"),
        ("SELECT value FROM secret(", "SELECT", "other"),
        ("SELECT value FROM secret (", "SELECT", "other"),
        ("SELECT value FROM secret,", "SELECT", "other"),
        ("INSERT INTO secret(id) VALUES (:id)", "INSERT", "secret"),
        ("INSERT INTO secret (id) VALUES (:id)", "INSERT", "secret"),
        ("INSERT INTO secret VALUES (:id)", "INSERT", "secret"),
        ("INSERT INTO secret(", "INSERT", "other"),
        ("INSERT INTO secret (", "INSERT", "other"),
        ("INSERT INTO secret() VALUES (:id)", "INSERT", "other"),
        ("UPDATE secret SET value = :value", "UPDATE", "secret"),
        ("UPDATE secret(", "UPDATE", "other"),
        ("UPDATE secret (", "UPDATE", "other"),
        ("UPDATE secret, SET value = :value", "UPDATE", "other"),
        ("DELETE FROM secret WHERE id = :id", "DELETE", "secret"),
        ("DELETE FROM secret(", "DELETE", "other"),
        ("DELETE FROM secret (", "DELETE", "other"),
        ("DELETE FROM secret,", "DELETE", "other"),
        (
            "MERGE INTO secret USING workspace ON secret.id = workspace.id",
            "MERGE",
            "secret",
        ),
        ("MERGE INTO secret(", "MERGE", "other"),
        ("MERGE INTO secret (", "MERGE", "other"),
        ("MERGE INTO secret, USING workspace", "MERGE", "other"),
        ("CREATE TABLE secret(id INTEGER)", "CREATE", "secret"),
        ("CREATE TABLE secret (id INTEGER)", "CREATE", "secret"),
        ("CREATE TABLE secret(", "CREATE", "other"),
        ("CREATE TABLE secret (", "CREATE", "other"),
        ("CREATE TABLE secret()", "CREATE", "other"),
        ("ALTER TABLE secret ADD COLUMN value TEXT", "ALTER", "secret"),
        ("ALTER TABLE secret(", "ALTER", "other"),
        ("ALTER TABLE secret (", "ALTER", "other"),
        ("ALTER TABLE secret, ADD COLUMN value TEXT", "ALTER", "other"),
        ("DROP TABLE secret;", "DROP", "secret"),
        ("DROP TABLE secret(", "DROP", "other"),
        ("DROP TABLE secret (", "DROP", "other"),
        ("DROP TABLE secret,", "DROP", "other"),
    ],
)
def test_normalized_sql_metadata_enforces_operation_specific_table_boundaries(
    statement: str,
    operation: str,
    expected_table: str,
) -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(statement, known_tables=KNOWN_TABLES) == {
        "db.system": "postgresql",
        "db.operation": operation,
        "db.table": expected_table,
    }


@pytest.mark.parametrize(
    ("statement", "operation"),
    [
        ("SELECT 'FROM secret'", "SELECT"),
        ("SELECT 1 UPDATE secret", "SELECT"),
        ("SELECT 1 /* FROM secret */", "SELECT"),
        ("SELECT 1 -- FROM secret\n", "SELECT"),
        ('SELECT value FROM "secret"', "SELECT"),
        ("INSERT secret (id) VALUES (:id)", "INSERT"),
        ("INSERT /* INTO secret */ INTO workspace (id) VALUES (:id)", "INSERT"),
        ("UPDATE 'secret' SET value = :value", "UPDATE"),
        ("DELETE secret WHERE id = :id", "DELETE"),
        ("MERGE secret USING workspace ON secret.id = workspace.id", "MERGE"),
        ("CREATE INDEX secret ON workspace (id)", "CREATE"),
        ("ALTER secret ADD COLUMN value TEXT", "ALTER"),
        ("DROP VIEW secret", "DROP"),
    ],
)
def test_normalized_sql_metadata_rejects_non_positional_table_canaries(
    statement: str, operation: str
) -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(statement, known_tables=KNOWN_TABLES) == {
        "db.system": "postgresql",
        "db.operation": operation,
        "db.table": "other",
    }


def test_normalized_sql_metadata_rejects_a_leading_comment() -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(
        "/* SELECT value FROM secret */ SELECT value FROM workspace",
        known_tables=KNOWN_TABLES,
    ) == {
        "db.system": "postgresql",
        "db.operation": "other",
        "db.table": "other",
    }


def test_unknown_table_is_other_even_when_supplied_by_the_caller() -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(
        "SELECT * FROM attacker_supplied",
        known_tables=frozenset({"attacker_supplied"}),
    ) == {"db.system": "postgresql", "db.operation": "SELECT", "db.table": "other"}


def test_non_string_statement_is_not_coerced_or_retained() -> None:
    class HostileStatement:
        def __str__(self) -> str:
            raise AssertionError("hostile-statement-canary")

        def __repr__(self) -> str:
            raise AssertionError("hostile-repr-canary")

    normalize = sqlalchemy_observability.normalized_sql_metadata

    assert normalize(HostileStatement(), known_tables=KNOWN_TABLES) == {
        "db.system": "postgresql",
        "db.operation": "other",
        "db.table": "other",
    }


def test_parser_inspects_only_the_bounded_prefix() -> None:
    normalize = sqlalchemy_observability.normalized_sql_metadata
    statement = " " * 4_096 + "SELECT * FROM secret"

    assert normalize(statement, known_tables=KNOWN_TABLES) == {
        "db.system": "postgresql",
        "db.operation": "other",
        "db.table": "other",
    }


class _Manager:
    def __init__(
        self,
        *,
        fail_enter: bool = False,
        fail_exit: bool = False,
        lifecycle: list[str] | None = None,
    ) -> None:
        self.span = object()
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit
        self.lifecycle = lifecycle
        self.exit_args: list[tuple[object | None, object | None, object | None]] = []

    def __enter__(self) -> object:
        if self.fail_enter:
            raise RuntimeError("start failure")
        return self.span

    def __exit__(
        self, exc_type: object | None, exc: object | None, traceback: object | None
    ) -> None:
        if self.lifecycle is not None:
            self.lifecycle.append("exit")
        self.exit_args.append((exc_type, exc, traceback))
        if self.fail_exit:
            raise RuntimeError("end failure")


class _UndeletableExecutionContext:
    lifecycle: list[str]
    delete_attempts: int
    clear_attempts: int
    reject_clear: bool

    def __init__(self, lifecycle: list[str], *, reject_clear: bool = False) -> None:
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "delete_attempts", 0)
        object.__setattr__(self, "clear_attempts", 0)
        object.__setattr__(self, "reject_clear", reject_clear)

    def __setattr__(self, name: str, value: object) -> None:
        if name == sqlalchemy_observability._SQL_STATE_ATTR and value is None:
            object.__setattr__(self, "clear_attempts", self.clear_attempts + 1)
            self.lifecycle.append("clear")
            if self.reject_clear:
                raise RuntimeError("clear-state-canary")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name == sqlalchemy_observability._SQL_STATE_ATTR:
            self.delete_attempts += 1
            self.lifecycle.append("delete")
            raise RuntimeError("delete-state-canary")
        super().__delattr__(name)


def _listeners(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    callbacks: dict[str, Any] = {}

    def listen(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        callbacks[event_name] = callback

    monkeypatch.setattr(sqlalchemy_event, "listen", listen)
    install = sqlalchemy_observability.install_sqlalchemy_tracing
    install(_SYNC_ENGINE, KNOWN_TABLES, tracer=noop_tracer())
    assert set(callbacks) == {
        "before_cursor_execute",
        "after_cursor_execute",
        "handle_error",
    }
    return callbacks


def _before(
    callback: Any,
    execution_context: object,
    statement: object = "SELECT * FROM secret",
) -> None:
    callback(
        object(),
        object(),
        statement,
        {"token": "bind-canary"},
        execution_context,
        False,
    )


@pytest.mark.parametrize("failure_index", range(3))
def test_listener_registration_failure_is_contained_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, failure_index: int
) -> None:
    event_names = ["before_cursor_execute", "after_cursor_execute", "handle_error"]
    attempts: list[str] = []
    active: dict[str, Any] = {}
    removals: list[str] = []

    def listen(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        attempts.append(event_name)
        if len(attempts) - 1 == failure_index:
            raise RuntimeError(f"registration-{failure_index}-canary")
        active[event_name] = callback

    def remove(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        assert active[event_name] is callback
        removals.append(event_name)
        del active[event_name]

    monkeypatch.setattr(sqlalchemy_event, "listen", listen)
    monkeypatch.setattr(sqlalchemy_event, "remove", remove)

    sqlalchemy_observability.install_sqlalchemy_tracing(
        _SYNC_ENGINE,
        KNOWN_TABLES,
        tracer=noop_tracer(),
    )

    assert attempts == event_names[: failure_index + 1]
    assert removals == list(reversed(event_names[:failure_index]))
    assert active == {}


def test_listener_registration_rollback_failure_leaves_callbacks_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: dict[str, Any] = {}
    removals: list[str] = []
    span_starts: list[str] = []

    def listen(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        if event_name == "handle_error":
            raise RuntimeError("registration-canary")
        callbacks[event_name] = callback

    def remove(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        assert callbacks[event_name] is callback
        removals.append(event_name)
        raise RuntimeError("rollback-canary")

    def start_span(*args: object, **kwargs: object) -> _Manager:
        span_starts.append("started")
        return _Manager()

    monkeypatch.setattr(sqlalchemy_event, "listen", listen)
    monkeypatch.setattr(sqlalchemy_event, "remove", remove)
    monkeypatch.setattr(sqlalchemy_observability, "safe_span", start_span)

    sqlalchemy_observability.install_sqlalchemy_tracing(
        _SYNC_ENGINE,
        KNOWN_TABLES,
        tracer=noop_tracer(),
    )

    execution_context = SimpleNamespace()
    _before(callbacks["before_cursor_execute"], execution_context)
    callbacks["after_cursor_execute"](
        object(), object(), "raw-sql-canary", {"bind": "bind-canary"}, execution_context, False
    )

    assert removals == ["after_cursor_execute", "before_cursor_execute"]
    assert span_starts == []
    assert not hasattr(execution_context, sqlalchemy_observability._SQL_STATE_ATTR)


def test_listener_success_removes_closed_state_and_ends_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    monkeypatch.setattr(sqlalchemy_observability, "safe_span", lambda *args, **kwargs: manager)
    callbacks = _listeners(monkeypatch)
    execution_context = SimpleNamespace()

    _before(callbacks["before_cursor_execute"], execution_context)
    state_name = sqlalchemy_observability._SQL_STATE_ATTR
    state = getattr(execution_context, state_name)
    state_type = sqlalchemy_observability._SQLSpanState
    assert isinstance(state, state_type)
    assert [field.name for field in fields(state)] == ["manager", "span", "closed"]
    assert vars(state) == {"manager": manager, "span": manager.span, "closed": False}

    callbacks["after_cursor_execute"](
        object(),
        object(),
        "raw-sql-canary",
        {"token": "bind-canary"},
        execution_context,
        False,
    )
    callbacks["after_cursor_execute"](
        object(),
        object(),
        "raw-sql-canary",
        {"token": "bind-canary"},
        execution_context,
        False,
    )

    assert not hasattr(execution_context, state_name)
    assert state.closed is True
    assert manager.exit_args == [(None, None, None)]


@pytest.mark.parametrize("reject_clear", [False, True])
def test_listener_success_closes_exactly_once_when_state_deletion_fails(
    monkeypatch: pytest.MonkeyPatch, reject_clear: bool
) -> None:
    lifecycle: list[str] = []
    manager = _Manager(lifecycle=lifecycle)
    monkeypatch.setattr(sqlalchemy_observability, "safe_span", lambda *args, **kwargs: manager)
    callbacks = _listeners(monkeypatch)
    execution_context = _UndeletableExecutionContext(
        lifecycle,
        reject_clear=reject_clear,
    )

    _before(
        callbacks["before_cursor_execute"],
        execution_context,
        "SELECT value FROM secret /* raw-sql-canary */",
    )
    state_name = sqlalchemy_observability._SQL_STATE_ATTR
    state = getattr(execution_context, state_name)
    callbacks["after_cursor_execute"](
        object(),
        object(),
        "raw-sql-canary",
        {"bind": "bind-canary"},
        execution_context,
        False,
    )
    callbacks["after_cursor_execute"](
        object(), object(), "raw-sql-canary", (), execution_context, False
    )

    assert lifecycle == ["delete", "clear", "exit", "delete", "clear"]
    assert execution_context.delete_attempts == 2
    assert execution_context.clear_attempts == 2
    if reject_clear:
        assert getattr(execution_context, state_name) is state
    else:
        assert getattr(execution_context, state_name) is None
    assert state.closed is True
    assert manager.exit_args == [(None, None, None)]
    assert "raw-sql-canary" not in repr(vars(state))
    assert "bind-canary" not in repr(vars(state))


def test_listener_error_records_only_safe_error_and_ends_without_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    captured: list[Any] = []
    monkeypatch.setattr(sqlalchemy_observability, "safe_span", lambda *args, **kwargs: manager)
    monkeypatch.setattr(
        sqlalchemy_observability,
        "record_span_error",
        lambda span, error: captured.append((span, error)),
    )
    callbacks = _listeners(monkeypatch)
    execution_context = SimpleNamespace()
    _before(callbacks["before_cursor_execute"], execution_context)
    original = RuntimeError("raw-exception-canary")

    result = callbacks["handle_error"](
        SimpleNamespace(execution_context=execution_context, original_exception=original)
    )

    assert result is None
    assert not hasattr(execution_context, sqlalchemy_observability._SQL_STATE_ATTR)
    assert manager.exit_args == [(None, None, None)]
    assert len(captured) == 1
    span, error = captured[0]
    assert span is manager.span
    assert error.type == "RuntimeError"
    assert error.code is SafeErrorCode.INTERNAL_ERROR
    assert "raw-exception-canary" not in repr(vars(error))


@pytest.mark.parametrize("record_fails", [False, True])
@pytest.mark.parametrize("reject_clear", [False, True])
def test_listener_error_closes_exactly_once_when_state_deletion_fails(
    monkeypatch: pytest.MonkeyPatch,
    record_fails: bool,
    reject_clear: bool,
) -> None:
    lifecycle: list[str] = []
    manager = _Manager(lifecycle=lifecycle)
    captured: list[tuple[object, object]] = []

    def record(span: object, error: object) -> None:
        lifecycle.append("record")
        captured.append((span, error))
        if record_fails:
            raise RuntimeError("record-failure-canary")

    monkeypatch.setattr(sqlalchemy_observability, "safe_span", lambda *args, **kwargs: manager)
    monkeypatch.setattr(sqlalchemy_observability, "record_span_error", record)
    callbacks = _listeners(monkeypatch)
    execution_context = _UndeletableExecutionContext(
        lifecycle,
        reject_clear=reject_clear,
    )
    _before(
        callbacks["before_cursor_execute"],
        execution_context,
        "SELECT value FROM secret /* raw-sql-canary */",
    )
    state = getattr(execution_context, sqlalchemy_observability._SQL_STATE_ATTR)
    original = RuntimeError("raw-exception-canary")
    exception_context = SimpleNamespace(
        execution_context=execution_context,
        original_exception=original,
    )

    assert callbacks["handle_error"](exception_context) is None
    assert callbacks["handle_error"](exception_context) is None

    assert exception_context.original_exception is original
    assert lifecycle == [
        "delete",
        "clear",
        "record",
        "exit",
        "delete",
        "clear",
    ]
    assert execution_context.delete_attempts == 2
    assert execution_context.clear_attempts == 2
    state_name = sqlalchemy_observability._SQL_STATE_ATTR
    if reject_clear:
        assert getattr(execution_context, state_name) is state
    else:
        assert getattr(execution_context, state_name) is None
    assert state.closed is True
    assert manager.exit_args == [(None, None, None)]
    assert len(captured) == 1
    assert captured[0][0] is manager.span
    assert "raw-sql-canary" not in repr(vars(state))
    assert "raw-exception-canary" not in repr(vars(state))


@pytest.mark.parametrize("failure", ["start", "publish", "record", "end"])
def test_listener_instrumentation_failures_are_contained(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    manager = _Manager(fail_enter=failure == "start", fail_exit=failure == "end")
    monkeypatch.setattr(sqlalchemy_observability, "safe_span", lambda *args, **kwargs: manager)
    if failure == "record":

        def fail_record(span: object, error: object) -> None:
            raise RuntimeError("record failure")

        monkeypatch.setattr(sqlalchemy_observability, "record_span_error", fail_record)
    callbacks = _listeners(monkeypatch)

    if failure == "publish":

        class ExecutionContext:
            def __setattr__(self, name: str, value: object) -> None:
                raise RuntimeError("publish failure")

        execution_context: object = ExecutionContext()
    else:
        execution_context = SimpleNamespace()

    _before(callbacks["before_cursor_execute"], execution_context)
    if failure == "record":
        assert (
            callbacks["handle_error"](
                SimpleNamespace(
                    execution_context=execution_context,
                    original_exception=RuntimeError("database failure"),
                )
            )
            is None
        )
    else:
        callbacks["after_cursor_execute"](
            object(), object(), "SELECT 1", (), execution_context, False
        )

    assert not hasattr(execution_context, sqlalchemy_observability._SQL_STATE_ATTR)
    if failure == "publish":
        assert manager.exit_args == [(None, None, None)]


def test_listener_states_are_independent_when_executions_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managers = [_Manager(), _Manager()]
    monkeypatch.setattr(
        sqlalchemy_observability,
        "safe_span",
        lambda *args, **kwargs: managers.pop(0),
    )
    callbacks = _listeners(monkeypatch)
    outer_context = SimpleNamespace()
    inner_context = SimpleNamespace()

    _before(callbacks["before_cursor_execute"], outer_context)
    outer_state = getattr(outer_context, sqlalchemy_observability._SQL_STATE_ATTR)
    _before(callbacks["before_cursor_execute"], inner_context)
    inner_state = getattr(inner_context, sqlalchemy_observability._SQL_STATE_ATTR)
    callbacks["after_cursor_execute"](object(), object(), "", (), inner_context, False)
    callbacks["after_cursor_execute"](object(), object(), "", (), outer_context, False)

    assert outer_state is not inner_state
    assert outer_state.closed is True
    assert inner_state.closed is True


def test_handle_error_without_execution_context_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _listeners(monkeypatch)

    assert callbacks["handle_error"](SimpleNamespace(execution_context=None)) is None
