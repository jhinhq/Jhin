"""Company identity model and PostgreSQL migration invariants."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import jhin_db.models as db_models
from jhin_db.migrate import alembic_config
from jhin_db.models import Agent, Team, Workspace
from jhin_domain import new_uuid7

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_ADMIN_DATABASE = "postgres"
PG_USER = "jhin"
PG_PASSWORD = "jhin"


def _company_models() -> tuple[type[Any], type[Any]]:
    """Return the new models while producing a clear RED before they exist."""
    assert hasattr(db_models, "AgentTeamMembership"), "AgentTeamMembership is missing"
    assert hasattr(db_models, "AgentRelationship"), "AgentRelationship is missing"
    return db_models.AgentTeamMembership, db_models.AgentRelationship


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    agent_team_membership, agent_relationship = _company_models()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    assert agent_team_membership.__table__ in Workspace.metadata.tables.values()
    assert agent_relationship.__table__ in Workspace.metadata.tables.values()
    Workspace.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _workspace(name: str) -> Workspace:
    return Workspace(id=new_uuid7(), name=name, slug=name.lower(), status="active")


def _team(workspace: Workspace, name: str) -> Team:
    return Team(id=new_uuid7(), workspace_id=workspace.id, name=name)


def _agent(workspace: Workspace, name: str, *, team_id: UUID | None = None) -> Agent:
    return Agent(
        id=new_uuid7(),
        workspace_id=workspace.id,
        team_id=team_id,
        name=name,
        slug=name.lower().replace(" ", "-"),
    )


def test_teamless_managerless_agent_has_safe_identity_defaults(sqlite_engine: Engine) -> None:
    workspace = _workspace("Solo")
    agent = _agent(workspace, "Solo Agent")

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add(agent)
        session.commit()
        session.refresh(agent)

        assert agent.team_id is None
        assert agent.manager_agent_id is None
        assert agent.public_purpose == ""
        assert agent.expertise_json == []
        assert agent.discoverability == "discoverable"
        assert agent.availability == "available"


def test_expertise_tags_are_bounded_before_persistence() -> None:
    workspace_id = uuid4()

    with pytest.raises(ValueError, match="at most 20"):
        Agent(
            workspace_id=workspace_id,
            name="Expert",
            slug="expert",
            expertise_json=[f"tag-{index}" for index in range(21)],
        )

    with pytest.raises(ValueError, match="1 to 64"):
        Agent(
            workspace_id=workspace_id,
            name="Expert",
            slug="expert",
            expertise_json=["x" * 65],
        )

    with pytest.raises(ValueError, match="strings"):
        Agent(
            workspace_id=workspace_id,
            name="Expert",
            slug="expert",
            expertise_json=[1],
        )


def test_mutated_expertise_tags_are_revalidated_at_persistence(sqlite_engine: Engine) -> None:
    workspace = _workspace("Mutable Expertise")
    agent = _agent(workspace, "Expert")
    agent.expertise_json = ["valid"]
    agent.expertise_json.append("x" * 65)

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add(agent)

        with pytest.raises(ValueError, match="1 to 64"):
            session.flush()


def test_agent_can_have_multiple_active_team_memberships(sqlite_engine: Engine) -> None:
    agent_team_membership, _ = _company_models()
    workspace = _workspace("Multiple")
    first_team = _team(workspace, "Engineering")
    second_team = _team(workspace, "Research")
    agent = _agent(workspace, "Builder")

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add_all([first_team, second_team])
        session.flush()
        session.add(agent)
        session.flush()
        session.add_all(
            [
                agent_team_membership(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    team_id=first_team.id,
                    is_primary=True,
                ),
                agent_team_membership(
                    workspace_id=workspace.id,
                    agent_id=agent.id,
                    team_id=second_team.id,
                    is_primary=False,
                ),
            ]
        )
        session.commit()

        assert session.query(agent_team_membership).count() == 2


def test_only_one_active_primary_membership_per_agent(sqlite_engine: Engine) -> None:
    agent_team_membership, _ = _company_models()
    workspace = _workspace("Primary")
    first_team = _team(workspace, "One")
    second_team = _team(workspace, "Two")
    agent = _agent(workspace, "Member")

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add_all([first_team, second_team])
        session.flush()
        session.add(agent)
        session.flush()
        ended_primary = agent_team_membership(
            workspace_id=workspace.id,
            agent_id=agent.id,
            team_id=first_team.id,
            is_primary=True,
            left_at=datetime.now(UTC),
        )
        active_primary = agent_team_membership(
            workspace_id=workspace.id,
            agent_id=agent.id,
            team_id=second_team.id,
            is_primary=True,
        )
        session.add_all([ended_primary, active_primary])
        session.flush()

        assert ended_primary.left_at is not None
        assert active_primary.left_at is None

        session.add(
            agent_team_membership(
                workspace_id=workspace.id,
                agent_id=agent.id,
                team_id=first_team.id,
                is_primary=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_active_agent_team_membership_pair_is_unique(sqlite_engine: Engine) -> None:
    agent_team_membership, _ = _company_models()
    workspace = _workspace("Pairs")
    team = _team(workspace, "Engineering")
    agent = _agent(workspace, "Member")

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add(team)
        session.flush()
        session.add(agent)
        session.flush()
        ended_membership = agent_team_membership(
            workspace_id=workspace.id,
            agent_id=agent.id,
            team_id=team.id,
            is_primary=False,
            left_at=datetime.now(UTC),
        )
        active_membership = agent_team_membership(
            workspace_id=workspace.id,
            agent_id=agent.id,
            team_id=team.id,
            is_primary=False,
        )
        session.add_all([ended_membership, active_membership])
        session.flush()

        assert ended_membership.left_at is not None
        assert active_membership.left_at is None

        session.add(
            agent_team_membership(
                workspace_id=workspace.id,
                agent_id=agent.id,
                team_id=team.id,
                is_primary=False,
            )
        )

        with pytest.raises(IntegrityError):
            session.flush()


def test_workspace_owned_edges_use_composite_foreign_keys() -> None:
    agent_team_membership, agent_relationship = _company_models()

    membership_fks = {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in agent_team_membership.__table__.foreign_key_constraints
    }
    relationship_fks = {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in agent_relationship.__table__.foreign_key_constraints
    }

    assert (("workspace_id", "agent_id"), ("workspace_id", "id")) in membership_fks
    assert (("workspace_id", "team_id"), ("workspace_id", "id")) in membership_fks
    assert (("workspace_id", "source_agent_id"), ("workspace_id", "id")) in relationship_fks
    assert (("workspace_id", "target_agent_id"), ("workspace_id", "id")) in relationship_fks


def test_relationship_kind_and_canonical_order_are_enforced(sqlite_engine: Engine) -> None:
    _, agent_relationship = _company_models()
    workspace = _workspace("Relationships")
    low_id = UUID("00000000-0000-7000-8000-000000000001")
    high_id = UUID("00000000-0000-7000-8000-000000000002")
    low_agent = _agent(workspace, "Low")
    high_agent = _agent(workspace, "High")
    low_agent.id = low_id
    high_agent.id = high_id
    workspace_id = workspace.id

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add_all([low_agent, high_agent])
        session.commit()

    invalid_relationships = [
        (
            high_id,
            low_id,
            "close_collaborator",
            "ck_agent_relationship_close_collaborator_order",
        ),
        (low_id, low_id, "advisor", "ck_agent_relationship_directed_not_self"),
        (low_id, high_id, "manager", "ck_agent_relationship_kind"),
    ]
    for source_id, target_id, kind, constraint_name in invalid_relationships:
        with Session(sqlite_engine) as session:
            session.add(
                agent_relationship(
                    workspace_id=workspace_id,
                    source_agent_id=source_id,
                    target_agent_id=target_id,
                    kind=kind,
                )
            )
            with pytest.raises(IntegrityError) as error:
                session.flush()
            assert constraint_name in str(error.value.orig)


def test_only_one_active_relationship_pair_and_kind(sqlite_engine: Engine) -> None:
    _, agent_relationship = _company_models()
    workspace = _workspace("Unique Relationships")
    source = _agent(workspace, "Source")
    target = _agent(workspace, "Target")

    with Session(sqlite_engine) as session:
        session.add(workspace)
        session.flush()
        session.add_all([source, target])
        session.flush()
        inactive_relationship = agent_relationship(
            workspace_id=workspace.id,
            source_agent_id=source.id,
            target_agent_id=target.id,
            kind="advisor",
            status="inactive",
        )
        active_relationship = agent_relationship(
            workspace_id=workspace.id,
            source_agent_id=source.id,
            target_agent_id=target.id,
            kind="advisor",
        )
        session.add_all([inactive_relationship, active_relationship])
        session.flush()

        assert inactive_relationship.status == "inactive"
        assert active_relationship.status == "active"

        session.add(
            agent_relationship(
                workspace_id=workspace.id,
                source_agent_id=source.id,
                target_agent_id=target.id,
                kind="advisor",
            )
        )

        with pytest.raises(IntegrityError):
            session.flush()


@dataclass(frozen=True)
class MigratedPostgres:
    asyncpg_dsn: str = field(repr=False)
    workspace_one_id: UUID
    workspace_two_id: UUID
    team_one_id: UUID
    team_two_id: UUID
    agent_one_id: UUID
    agent_two_id: UUID


async def _connect_admin() -> asyncpg.Connection[Any]:
    return await asyncpg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_ADMIN_DATABASE,
    )


async def _seed_legacy_rows(
    dsn: str,
    *,
    workspace_one_id: UUID,
    workspace_two_id: UUID,
    team_one_id: UUID,
    team_two_id: UUID,
    agent_one_id: UUID,
    agent_two_id: UUID,
) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        for workspace_id, name in (
            (workspace_one_id, "Workspace One"),
            (workspace_two_id, "Workspace Two"),
        ):
            await connection.execute(
                """
                INSERT INTO workspace (id, name, slug, status, default_timezone, settings_json)
                VALUES ($1, $2, $3, 'active', 'UTC', '{}'::jsonb)
                """,
                workspace_id,
                name,
                name.lower().replace(" ", "-"),
            )
        for team_id, workspace_id, name in (
            (team_one_id, workspace_one_id, "Team One"),
            (team_two_id, workspace_two_id, "Team Two"),
        ):
            await connection.execute(
                """
                INSERT INTO team
                    (id, workspace_id, name, description, color_token, icon)
                VALUES ($1, $2, $3, '', 'slate', 'users')
                """,
                team_id,
                workspace_id,
                name,
            )
        for agent_id, workspace_id, team_id, name in (
            (agent_one_id, workspace_one_id, team_one_id, "Agent One"),
            (agent_two_id, workspace_two_id, team_two_id, "Agent Two"),
        ):
            await connection.execute(
                """
                INSERT INTO agent (
                    id, workspace_id, team_id, name, slug, role_title, description,
                    system_prompt, status, autonomy_level, max_steps, max_run_minutes,
                    max_concurrent_runs, approval_policy_json, metadata_json
                )
                VALUES (
                    $1, $2, $3, $4, $5, '', '', '', 'active', 'supervised',
                    20, 30, 1, '[]'::jsonb, '{}'::jsonb
                )
                """,
                agent_id,
                workspace_id,
                team_id,
                name,
                name.lower().replace(" ", "-"),
            )
    finally:
        await connection.close()


async def _assert_company_identity_downgraded(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        assert (
            await connection.fetchval("SELECT to_regclass('public.agent_team_membership')::text")
            is None
        )
        assert (
            await connection.fetchval("SELECT to_regclass('public.agent_relationship')::text")
            is None
        )
        agent_columns = {
            row["column_name"]
            for row in await connection.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'agent'
                """
            )
        }
        assert "max_concurrent_runs" in agent_columns
        assert {
            "public_purpose",
            "expertise_json",
            "discoverability",
            "availability",
        }.isdisjoint(agent_columns)
        release_constraints = await connection.fetchval(
            """
            SELECT count(*)
            FROM pg_constraint
            WHERE conname IN ('uq_agent_workspace_id_id', 'uq_team_workspace_id_id')
            """
        )
        assert release_constraints == 0
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def migrated_postgres() -> Iterator[MigratedPostgres]:
    database_name = f"jhin_company_identity_{uuid4().hex}"

    async def _create_database() -> None:
        admin = await _connect_admin()
        try:
            await admin.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await admin.close()

    try:
        asyncio.run(_create_database())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL dev overlay is unavailable: {type(exc).__name__}")
    asyncpg_dsn = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{database_name}"
    sqlalchemy_url = asyncpg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    ids = MigratedPostgres(
        asyncpg_dsn=asyncpg_dsn,
        workspace_one_id=uuid4(),
        workspace_two_id=uuid4(),
        team_one_id=uuid4(),
        team_two_id=uuid4(),
        agent_one_id=uuid4(),
        agent_two_id=uuid4(),
    )
    try:
        config = alembic_config(sqlalchemy_url)
        command.upgrade(config, "0013")
        asyncio.run(
            _seed_legacy_rows(
                asyncpg_dsn,
                workspace_one_id=ids.workspace_one_id,
                workspace_two_id=ids.workspace_two_id,
                team_one_id=ids.team_one_id,
                team_two_id=ids.team_two_id,
                agent_one_id=ids.agent_one_id,
                agent_two_id=ids.agent_two_id,
            )
        )
        command.upgrade(config, "head")
        yield ids
        command.downgrade(config, "0013")
        asyncio.run(_assert_company_identity_downgraded(asyncpg_dsn))
    finally:

        async def _drop_database() -> None:
            cleanup_admin = await _connect_admin()
            try:
                await cleanup_admin.execute(
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
                )
            finally:
                await cleanup_admin.close()

        asyncio.run(_drop_database())


@pytest.mark.parametrize(
    ("edge", "constraint_name"),
    [
        ("membership", "fk_agent_team_membership_workspace_team"),
        ("relationship", "fk_agent_relationship_workspace_target_agent"),
    ],
)
async def test_postgres_rejects_cross_workspace_edges(
    migrated_postgres: MigratedPostgres, edge: str, constraint_name: str
) -> None:
    connection = await asyncpg.connect(migrated_postgres.asyncpg_dsn)
    try:
        values: tuple[Any, ...]
        if edge == "membership":
            statement = """
                INSERT INTO agent_team_membership
                    (id, workspace_id, agent_id, team_id, is_primary, role_label, joined_at)
                VALUES ($1, $2, $3, $4, false, '', $5)
            """
            values = (
                uuid4(),
                migrated_postgres.workspace_one_id,
                migrated_postgres.agent_one_id,
                migrated_postgres.team_two_id,
                datetime.now(UTC),
            )
        else:
            statement = """
                INSERT INTO agent_relationship
                    (id, workspace_id, source_agent_id, target_agent_id, kind, purpose, status)
                VALUES ($1, $2, $3, $4, 'advisor', '', 'active')
            """
            values = (
                uuid4(),
                migrated_postgres.workspace_one_id,
                migrated_postgres.agent_one_id,
                migrated_postgres.agent_two_id,
            )

        with pytest.raises(asyncpg.ForeignKeyViolationError) as error:
            await connection.execute(statement, *values)
        assert error.value.constraint_name == constraint_name
    finally:
        await connection.close()


async def test_migration_backfills_legacy_team_links_with_uuid7_and_identity_defaults(
    migrated_postgres: MigratedPostgres,
) -> None:
    connection = await asyncpg.connect(migrated_postgres.asyncpg_dsn)
    try:
        membership = await connection.fetchrow(
            """
            SELECT id, workspace_id, agent_id, team_id, is_primary, left_at
            FROM agent_team_membership
            WHERE agent_id = $1
            """,
            migrated_postgres.agent_one_id,
        )
        identity = await connection.fetchrow(
            """
            SELECT public_purpose, expertise_json, discoverability, availability
            FROM agent
            WHERE id = $1
            """,
            migrated_postgres.agent_one_id,
        )

        assert membership is not None
        assert membership["id"].version == 7
        assert membership["workspace_id"] == migrated_postgres.workspace_one_id
        assert membership["team_id"] == migrated_postgres.team_one_id
        assert membership["is_primary"] is True
        assert membership["left_at"] is None
        assert identity is not None
        assert {
            **dict(identity),
            "expertise_json": json.loads(identity["expertise_json"]),
        } == {
            "public_purpose": "",
            "expertise_json": [],
            "discoverability": "discoverable",
            "availability": "available",
        }
    finally:
        await connection.close()
