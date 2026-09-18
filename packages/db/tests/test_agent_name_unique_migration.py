"""0043 on an install that already holds duplicate agent names.

The chain from 0001 needs PostgreSQL, so this does not replay it. What it
does exercise is the part that can take a deploy down: an install where two
agents in one workspace answer to the same name — which is exactly the state
the reported ``PATCH`` left behind — and where ``CREATE UNIQUE INDEX`` would
therefore fail. The migration's own ``upgrade()`` runs against a SQLite
database built from the current models (with the new index dropped again, so
the duplicates can be seeded at all), and the promises checked are the ones
an admin will care about afterwards: the oldest keeps its name, nobody's slug
moves, every fix-up leaves an ``agent.renamed`` row, and the index exists at
the end.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jhin_db.base import Base
from jhin_db.models import Agent, AuditEvent, Workspace

MIGRATION = importlib.import_module("jhin_db.alembic.versions.20260906_0043_agent_name_unique")

_EPOCH = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def connection() -> Iterator[Connection]:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        # The state this migration exists to fix cannot be created while the
        # rule is already in place.
        conn.exec_driver_sql(f"DROP INDEX {MIGRATION.INDEX_NAME}")
        yield conn
    engine.dispose()


def _add_agent(conn: Connection, *, workspace_id: UUID, name: str, slug: str, minutes: int) -> UUID:
    agent = Agent(
        workspace_id=workspace_id,
        name=name,
        slug=slug,
        created_at=_EPOCH + timedelta(minutes=minutes),
        updated_at=_EPOCH + timedelta(minutes=minutes),
    )
    with Session(bind=conn) as session:
        session.add(agent)
        session.flush()
        agent_id = agent.id
        session.expunge_all()
    return agent_id


def _workspace(conn: Connection, slug: str) -> UUID:
    workspace = Workspace(name=slug, slug=slug)
    with Session(bind=conn) as session:
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
        session.expunge_all()
    return workspace_id


def _run(conn: Connection) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        MIGRATION.upgrade()


def _names(conn: Connection, workspace_id: UUID) -> list[tuple[str, str]]:
    with Session(bind=conn) as session:
        rows = session.execute(
            select(Agent.name, Agent.slug)
            .where(Agent.workspace_id == workspace_id)
            .order_by(Agent.created_at, Agent.id)
        ).all()
    return [(row[0], row[1]) for row in rows]


def test_the_oldest_keeps_the_name_and_the_later_ones_are_numbered(
    connection: Connection,
) -> None:
    """The reported state, exactly: ``PATCH`` named a second agent "QA
    Engineer", then "qa engineer" — one name to anybody reading a roster,
    three rows to the database."""
    workspace_id = _workspace(connection, "hq")
    _add_agent(connection, workspace_id=workspace_id, name="QA Engineer", slug="qa-1", minutes=0)
    _add_agent(connection, workspace_id=workspace_id, name="qa engineer", slug="qa-2", minutes=1)
    _add_agent(connection, workspace_id=workspace_id, name="QA Engineer", slug="qa-3", minutes=2)

    _run(connection)

    assert _names(connection, workspace_id) == [
        ("QA Engineer", "qa-1"),
        # Each row keeps the spelling somebody typed and gains the smallest
        # free suffix; the third has to skip 2, which the second took.
        ("qa engineer 2", "qa-2"),
        ("QA Engineer 3", "qa-3"),
    ]


def test_a_name_that_is_only_duplicated_in_another_workspace_is_left_alone(
    connection: Connection,
) -> None:
    """The rule is per workspace, like every other rule about agents."""
    first = _workspace(connection, "hq")
    second = _workspace(connection, "labs")
    _add_agent(connection, workspace_id=first, name="Scout", slug="scout", minutes=0)
    _add_agent(connection, workspace_id=second, name="Scout", slug="scout", minutes=1)

    _run(connection)

    assert _names(connection, first) == [("Scout", "scout")]
    assert _names(connection, second) == [("Scout", "scout")]


def test_every_fix_up_leaves_the_same_audit_row_a_rename_leaves(
    connection: Connection,
) -> None:
    """A rename nobody can see is the thing this whole feature refuses to
    do. A migration renaming somebody's agent is no different."""
    workspace_id = _workspace(connection, "hq")
    _add_agent(connection, workspace_id=workspace_id, name="Bisby", slug="bisby", minutes=0)
    later = _add_agent(
        connection, workspace_id=workspace_id, name="bisby", slug="bisby-2", minutes=1
    )

    _run(connection)

    with Session(bind=connection) as session:
        rows = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "agent.renamed")))
    assert len(rows) == 1
    assert rows[0].actor_type == "system"
    assert rows[0].target_id == later
    metadata = rows[0].metadata_json
    assert metadata["from"] == "bisby"
    assert metadata["to"] == "bisby 2"
    # The handle is the stable reference and does not move, here either.
    assert metadata["slug"] == "bisby-2"
    assert metadata["via"] == "migration 0043"
    assert metadata["requested_by_user_id"] is None


def test_the_index_is_created_and_refuses_the_next_duplicate(
    connection: Connection,
) -> None:
    workspace_id = _workspace(connection, "hq")
    _add_agent(connection, workspace_id=workspace_id, name="Scout", slug="scout", minutes=0)

    _run(connection)

    indexes = set(
        connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'agent'")
        ).scalars()
    )
    assert MIGRATION.INDEX_NAME in indexes
    with pytest.raises(IntegrityError):
        _add_agent(connection, workspace_id=workspace_id, name="scout", slug="scout-2", minutes=5)


def test_the_migration_is_idempotent_on_an_install_with_no_duplicates(
    connection: Connection,
) -> None:
    """Nothing to fix means nothing written: no rename, no audit row."""
    workspace_id = _workspace(connection, "hq")
    _add_agent(connection, workspace_id=workspace_id, name="Scout", slug="scout", minutes=0)
    _add_agent(connection, workspace_id=workspace_id, name="Bisby", slug="bisby", minutes=1)

    _run(connection)

    assert _names(connection, workspace_id) == [("Scout", "scout"), ("Bisby", "bisby")]
    with Session(bind=connection) as session:
        assert session.scalar(select(AuditEvent.id)) is None
