"""Statement-free, fail-open SQLAlchemy tracing integration."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import Span, SpanKind, Tracer
from sqlalchemy import event

from jhin_observability.context import record_span_error, safe_span
from jhin_observability.errors import SafeErrorCode, safe_error
from jhin_observability.registry import DB_TABLE_VALUES

_SQL_PREFIX_LIMIT = 4_096
_SQL_STATE_ATTR = "_jhin_observability_sql_span_state"
_OPERATION_PATTERN = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP)\b",
    re.IGNORECASE,
)
_TABLE_PATTERN = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)


@dataclass
class _SQLSpanState:
    manager: AbstractContextManager[Span]
    span: Span
    closed: bool = False


def normalized_sql_metadata(
    statement: object,
    *,
    known_tables: frozenset[str],
) -> dict[str, str]:
    """Return closed SQL metadata without coercing or retaining the statement."""
    operation = "other"
    table = "other"
    if isinstance(statement, str):
        prefix = statement[:_SQL_PREFIX_LIMIT]
        operation_match = _OPERATION_PATTERN.match(prefix)
        if operation_match is not None:
            operation = operation_match.group(1).upper()
            table_match = _TABLE_PATTERN.search(prefix)
            if table_match is not None:
                candidate = table_match.group(1).lower()
                normalized_known_tables = frozenset(name.lower() for name in known_tables)
                if candidate in normalized_known_tables and candidate in DB_TABLE_VALUES:
                    table = candidate
    return {
        "db.system": "postgresql",
        "db.operation": operation,
        "db.table": table,
    }


def _exit_manager(manager: AbstractContextManager[Span]) -> None:
    with suppress(Exception):
        manager.__exit__(None, None, None)


def _take_state(execution_context: object | None) -> _SQLSpanState | None:
    if execution_context is None:
        return None
    try:
        state = getattr(execution_context, _SQL_STATE_ATTR)
    except Exception:
        return None
    try:
        delattr(execution_context, _SQL_STATE_ATTR)
    except Exception:
        return None
    if not isinstance(state, _SQLSpanState) or state.closed:
        return None
    state.closed = True
    return state


def install_sqlalchemy_tracing(
    sync_engine: object,
    known_tables: frozenset[str],
    *,
    tracer: Tracer,
) -> None:
    """Install statement-free SQL listeners on a synchronous engine facade."""

    def before_cursor_execute(
        _connection: object,
        _cursor: object,
        statement: object,
        _parameters: object,
        execution_context: object,
        _executemany: bool,
    ) -> None:
        manager: AbstractContextManager[Span] | None = None
        try:
            metadata = normalized_sql_metadata(statement, known_tables=known_tables)
            manager = safe_span(
                "db.operation",
                tracer=tracer,
                kind=SpanKind.CLIENT,
                attributes=metadata,
            )
            span = manager.__enter__()
            setattr(
                execution_context,
                _SQL_STATE_ATTR,
                _SQLSpanState(manager=manager, span=span),
            )
        except Exception:
            if manager is not None:
                _exit_manager(manager)

    def after_cursor_execute(
        _connection: object,
        _cursor: object,
        _statement: object,
        _parameters: object,
        execution_context: object,
        _executemany: bool,
    ) -> None:
        state = _take_state(execution_context)
        if state is not None:
            _exit_manager(state.manager)

    def handle_error(exception_context: Any) -> None:
        try:
            execution_context = getattr(exception_context, "execution_context", None)
        except Exception:
            return None
        state = _take_state(execution_context)
        if state is None:
            return None
        try:
            try:
                original_exception = exception_context.original_exception
                if isinstance(original_exception, BaseException):
                    record_span_error(
                        state.span,
                        safe_error(
                            original_exception,
                            code=SafeErrorCode.INTERNAL_ERROR,
                        ),
                    )
            except Exception:
                pass
        finally:
            _exit_manager(state.manager)
        return None

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    event.listen(sync_engine, "after_cursor_execute", after_cursor_execute)
    event.listen(sync_engine, "handle_error", handle_error)


__all__ = ["install_sqlalchemy_tracing", "normalized_sql_metadata"]
