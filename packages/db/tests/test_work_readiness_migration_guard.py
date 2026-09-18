"""Rollback must not discard durable work or required-answer provenance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from jhin_db.migrate import alembic_config


@pytest.mark.parametrize(
    "fixture_sql",
    [
        "INSERT INTO agent_schedule VALUES (1)",
        "INSERT INTO schedule_occurrence VALUES (1)",
        "INSERT INTO user_question VALUES (TRUE, '', 'text')",
        "INSERT INTO user_question VALUES (FALSE, 'confirmed_url', 'text')",
        "INSERT INTO user_question VALUES (FALSE, '', 'url')",
    ],
)
def test_downgrade_refuses_retained_state_before_any_ddl(monkeypatch, fixture_sql):
    revision = ScriptDirectory.from_config(alembic_config("sqlite://")).get_revision("0049")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE agent_schedule (id INTEGER)"))
        connection.execute(sa.text("CREATE TABLE schedule_occurrence (id INTEGER)"))
        connection.execute(
            sa.text(
                "CREATE TABLE user_question (required BOOLEAN, input_key TEXT, value_type TEXT)"
            )
        )
        connection.execute(sa.text(fixture_sql))
        drop_table, drop_column = Mock(), Mock()
        monkeypatch.setattr(
            revision.module,
            "op",
            SimpleNamespace(
                get_bind=lambda: connection, drop_table=drop_table, drop_column=drop_column
            ),
        )
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            revision.module.downgrade()
        drop_table.assert_not_called()
        drop_column.assert_not_called()
    engine.dispose()


def test_downgrade_allows_only_legacy_optional_question_state(monkeypatch):
    revision = ScriptDirectory.from_config(alembic_config("sqlite://")).get_revision("0049")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE agent_schedule (id INTEGER)"))
        connection.execute(sa.text("CREATE TABLE schedule_occurrence (id INTEGER)"))
        connection.execute(
            sa.text(
                "CREATE TABLE user_question (required BOOLEAN, input_key TEXT, value_type TEXT)"
            )
        )
        connection.execute(sa.text("INSERT INTO user_question VALUES (FALSE, '', 'text')"))
        drop_table, drop_column = Mock(), Mock()
        monkeypatch.setattr(
            revision.module,
            "op",
            SimpleNamespace(
                get_bind=lambda: connection, drop_table=drop_table, drop_column=drop_column
            ),
        )
        revision.module.downgrade()
        assert drop_table.call_count == 2
        assert drop_column.call_count == 3
    engine.dispose()
