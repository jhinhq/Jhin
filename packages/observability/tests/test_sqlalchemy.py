"""Statement-free SQLAlchemy tracing tests."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

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
    def __init__(self, *, fail_enter: bool = False, fail_exit: bool = False) -> None:
        self.span = object()
        self.fail_enter = fail_enter
        self.fail_exit = fail_exit
        self.exit_args: list[tuple[object | None, object | None, object | None]] = []

    def __enter__(self) -> object:
        if self.fail_enter:
            raise RuntimeError("start failure")
        return self.span

    def __exit__(
        self, exc_type: object | None, exc: object | None, traceback: object | None
    ) -> None:
        self.exit_args.append((exc_type, exc, traceback))
        if self.fail_exit:
            raise RuntimeError("end failure")


def _listeners(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    callbacks: dict[str, Any] = {}

    def listen(engine: object, event_name: str, callback: Any) -> None:
        assert engine is _SYNC_ENGINE
        callbacks[event_name] = callback

    event = sqlalchemy_observability.event
    monkeypatch.setattr(event, "listen", listen)
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
