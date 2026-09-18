"""The managed app namespace migration preserves owners and enforces uniqueness."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jhin_db.base import Base
from jhin_db.models import Connection, Workspace

MIGRATION = importlib.import_module(
    "jhin_db.alembic.versions.20260908_0044_composio_server_slug_unique"
)


def test_duplicate_names_are_repaired_without_renaming_original_distinct_names():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        with Session(bind=conn) as db:
            workspace = Workspace(name="Test", slug="test")
            db.add(workspace)
            db.flush()
            workspace_id = workspace.id
            original_ids = []
            for position, slug in enumerate(("slack", "slack", "slack_2")):
                row = Connection(
                    workspace_id=workspace_id,
                    name=f"App {position}",
                    connector_type="composio",
                    auth_type="managed",
                    status="disabled" if position == 0 else "active",
                    config_json={"server_slug": slug, "toolkit": "slack"},
                    created_at=datetime(2026, 9, 8, tzinfo=UTC) + timedelta(seconds=position),
                )
                db.add(row)
                db.flush()
                original_ids.append(row.id)
            db.expunge_all()
        with Operations.context(MigrationContext.configure(conn)):
            MIGRATION.upgrade()
        with Session(bind=conn) as db:
            rows = db.scalars(select(Connection).order_by(Connection.created_at)).all()
            assert [row.id for row in rows] == original_ids
            assert [row.config_json["server_slug"] for row in rows] == [
                "slack",
                "slack_3",
                "slack_2",
            ]
            assert rows[0].status == "disabled"
            assert all(row.config_json["toolkit"] == "slack" for row in rows)
            db.add(
                Connection(
                    workspace_id=workspace_id,
                    name="Another",
                    connector_type="composio",
                    auth_type="managed",
                    config_json={"server_slug": "slack"},
                )
            )
            with pytest.raises(IntegrityError):
                db.flush()
    engine.dispose()


def test_migration_continues_existing_head():
    assert MIGRATION.revision == "0044"
    assert MIGRATION.down_revision == "0043"
