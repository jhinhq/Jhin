"""Database observability integration tests."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from sqlalchemy import text

import jhin_db.engine as engine_module
from jhin_db import create_engine
from jhin_observability import ObservabilityNotInitializedError, get_runtime, noop_tracer

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class _Telemetry:
    tracer: Tracer
    spans: InMemorySpanExporter


@pytest.fixture
def telemetry() -> Iterator[_Telemetry]:
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": "db-test",
                "service.version": "test",
                "deployment.environment": "test",
            }
        )
    )
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield _Telemetry(provider.get_tracer("db-tests"), exporter)
    finally:
        provider.shutdown()


@pytest.fixture
def spans(telemetry: _Telemetry) -> InMemorySpanExporter:
    return telemetry.spans


def _serialized_span(span: ReadableSpan) -> str:
    parent = span.parent
    return json.dumps(
        {
            "name": span.name,
            "context": {
                "trace_id": format(span.context.trace_id, "032x") if span.context else None,
                "span_id": format(span.context.span_id, "016x") if span.context else None,
            },
            "parent": {
                "trace_id": format(parent.trace_id, "032x") if parent else None,
                "span_id": format(parent.span_id, "016x") if parent else None,
            },
            "resource": dict(span.resource.attributes),
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
            "status": {
                "code": span.status.status_code.name,
                "description": span.status.description,
            },
        },
        default=str,
        sort_keys=True,
    )


async def _prepare_secret_table(engine: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE secret (token TEXT PRIMARY KEY, value TEXT NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO secret (token, value) VALUES ('known-token', 'known-value')")
        )


def test_engine_exposes_safe_tracing_controls() -> None:
    parameters = inspect.signature(create_engine).parameters
    assert "trace_sql" in parameters
    assert "tracer" in parameters
    assert "echo" not in parameters


def test_database_url_is_only_a_constructor_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+asyncpg://secret-user:secret-pass@db-canary/jhin"
    sync_engine = object()
    fake_engine = SimpleNamespace(sync_engine=sync_engine)
    constructor_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    listener_calls: list[tuple[object, frozenset[str], object]] = []

    def construct(*args: object, **kwargs: object) -> object:
        constructor_calls.append((args, kwargs))
        return fake_engine

    def install(engine: object, known_tables: frozenset[str], *, tracer: object) -> None:
        listener_calls.append((engine, known_tables, tracer))

    monkeypatch.setattr(engine_module, "create_async_engine", construct)
    monkeypatch.setattr(engine_module, "install_sqlalchemy_tracing", install, raising=False)
    tracer = noop_tracer()

    assert create_engine(database_url, tracer=tracer) is fake_engine
    assert constructor_calls == [((database_url,), {"echo": False, "pool_pre_ping": True})]
    assert len(listener_calls) == 1
    assert listener_calls == [(sync_engine, listener_calls[0][1], tracer)]
    assert database_url not in repr(listener_calls)


def test_echo_cannot_be_enabled_through_the_public_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        engine_module,
        "create_async_engine",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(TypeError):
        create_engine("sqlite+aiosqlite:///:memory:", echo=True)  # type: ignore[call-arg]

    assert calls == []


@pytest.mark.asyncio
async def test_sql_span_has_closed_metadata_and_correct_parent(
    telemetry: _Telemetry,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sql_canary = "sql-token-canary-3c1440"
    bind_canary = "bind-value-canary-9120b7"
    engine = create_engine("sqlite+aiosqlite:///:memory:", tracer=telemetry.tracer)
    try:
        await _prepare_secret_table(engine)
        spans.clear()
        capsys.readouterr()

        with telemetry.tracer.start_as_current_span("outer") as outer:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(f"SELECT value FROM secret WHERE token=:token /* {sql_canary} */"),
                    {"token": bind_canary},
                )
                assert result.scalar_one_or_none() is None

        db_spans = [span for span in spans.get_finished_spans() if span.name == "db.operation"]
        assert len(db_spans) == 1
        db_span = db_spans[0]
        assert dict(db_span.attributes or {}) == {
            "db.system": "postgresql",
            "db.operation": "SELECT",
            "db.table": "secret",
        }
        assert db_span.parent is not None
        assert db_span.parent.span_id == outer.get_span_context().span_id
        rendered = _serialized_span(db_span)
        captured = capsys.readouterr()
        complete_output = rendered + captured.out + captured.err
        assert sql_canary not in complete_output
        assert bind_canary not in complete_output
    finally:
        await engine.dispose()

    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_error_span_is_safe_and_a_following_query_succeeds(
    telemetry: _Telemetry,
    spans: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sql_canary = "error-sql-canary-80e1"
    exception_canary = "missing-table-canary-c778"
    engine = create_engine("sqlite+aiosqlite:///:memory:", tracer=telemetry.tracer)
    try:
        await _prepare_secret_table(engine)
        spans.clear()
        capsys.readouterr()
        async with engine.connect() as connection:
            with pytest.raises(Exception, match="attacker_supplied"):
                await connection.execute(
                    text(
                        "SELECT value FROM attacker_supplied "
                        f"WHERE token='{exception_canary}' /* {sql_canary} */"
                    )
                )
            result = await connection.execute(text("SELECT value FROM secret"))
            assert result.scalar_one() == "known-value"

        db_spans = [span for span in spans.get_finished_spans() if span.name == "db.operation"]
        assert len(db_spans) == 2
        failed, succeeded = db_spans
        assert dict(failed.attributes or {}) == {
            "db.system": "postgresql",
            "db.operation": "SELECT",
            "db.table": "other",
            "error.type": "OperationalError",
            "error.code": "internal_error",
        }
        assert failed.events == ()
        assert failed.status.description is None
        assert dict(succeeded.attributes or {}) == {
            "db.system": "postgresql",
            "db.operation": "SELECT",
            "db.table": "secret",
        }
        captured = capsys.readouterr()
        rendered = "".join(_serialized_span(span) for span in db_spans)
        complete_output = rendered + captured.out + captured.err
        assert sql_canary not in complete_output
        assert exception_canary not in complete_output
    finally:
        await engine.dispose()

    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_concurrent_executions_have_independent_exactly_once_spans(
    telemetry: _Telemetry, spans: InMemorySpanExporter
) -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:", tracer=telemetry.tracer)
    try:
        await _prepare_secret_table(engine)
        spans.clear()

        async def query() -> str:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT value FROM secret"))
                return str(result.scalar_one())

        with telemetry.tracer.start_as_current_span("concurrent-parent") as parent:
            assert await asyncio.gather(query(), query()) == ["known-value", "known-value"]

        db_spans = [span for span in spans.get_finished_spans() if span.name == "db.operation"]
        assert len(db_spans) == 2
        assert all(
            span.parent is not None and span.parent.span_id == parent.get_span_context().span_id
            for span in db_spans
        )
        assert len({span.context.span_id for span in db_spans if span.context}) == 2
    finally:
        await engine.dispose()

    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_database_package_is_safe_before_observability_bootstrap() -> None:
    with pytest.raises(ObservabilityNotInitializedError):
        get_runtime()
    engine = create_engine("sqlite+aiosqlite:///:memory:", trace_sql=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def test_seed_explicitly_selects_package_noop_tracer_before_bootstrap() -> None:
    tree = ast.parse((REPO_ROOT / "apps/api/src/jhin_api/seed.py").read_text())
    create_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "create_engine"
    ]
    assert len(create_calls) == 1
    tracer = next(keyword for keyword in create_calls[0].keywords if keyword.arg == "tracer")
    assert ast.unparse(tracer.value) == "noop_tracer()"
