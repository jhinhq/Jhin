"""StdUuid normalizes driver UUID subclasses to exact stdlib UUIDs."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql, sqlite

from jhin_db.columns import StdUuid


class _DriverUuid(UUID):
    """Stands in for asyncpg's ``pgproto.UUID`` subclass."""


def test_result_value_subclass_becomes_exact_builtin_uuid() -> None:
    column = StdUuid()
    raw = _DriverUuid(str(uuid4()))
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        value = column.process_result_value(raw, dialect)
        assert type(value) is UUID
        assert value == raw


def test_exact_uuid_and_none_pass_through_unchanged() -> None:
    column = StdUuid()
    exact = uuid4()
    dialect = postgresql.dialect()
    assert column.process_result_value(exact, dialect) is exact
    assert column.process_result_value(None, dialect) is None
    assert column.process_bind_param(exact, dialect) is exact
    assert column.process_bind_param(None, dialect) is None


def test_bind_param_normalizes_subclass() -> None:
    column = StdUuid()
    raw = _DriverUuid(str(uuid4()))
    bound = column.process_bind_param(raw, postgresql.dialect())
    assert type(bound) is UUID and bound == raw
