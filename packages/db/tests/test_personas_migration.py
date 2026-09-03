"""0036 (personas) against a real PostgreSQL: the table and the agent column
appear, every existing workspace receives the shipped cast, every existing
agent receives the self-persona grant unless somebody already decided
otherwise, and the downgrade takes it all back out.

Runs on the dev overlay's database like the company identity migration
test, and skips the same way when that database is not reachable. SQLite
cannot host this round trip: the chain uses Postgres-only DDL from 0001 on,
and 0036 itself adds a foreign key with ALTER, which SQLite has no support
for outside batch mode (0023 made the same choice).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command

from jhin_db.migrate import alembic_config

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_ADMIN_DATABASE = "postgres"
PG_USER = "jhin"
PG_PASSWORD = "jhin"

PERSONA_SELF_CAPABILITY = "organization.persona.self"
SHIPPED_CARDS = 12


@dataclass(frozen=True)
class MigratedPostgres:
    asyncpg_dsn: str = field(repr=False)
    workspace_one_id: UUID
    workspace_two_id: UUID
    # Workspace one: an agent with no grants at all, and one that already
    # carries a deny for the persona capability.
    fresh_agent_id: UUID
    denied_agent_id: UUID


async def _connect_admin() -> asyncpg.Connection[Any]:
    return await asyncpg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_ADMIN_DATABASE,
    )


async def _seed_pre_persona_rows(dsn: str, ids: MigratedPostgres) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        for workspace_id, name in (
            (ids.workspace_one_id, "Workspace One"),
            (ids.workspace_two_id, "Workspace Two"),
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
        for agent_id, name in (
            (ids.fresh_agent_id, "Fresh Agent"),
            (ids.denied_agent_id, "Denied Agent"),
        ):
            await connection.execute(
                """
                INSERT INTO agent (
                    id, workspace_id, name, slug, role_title, description,
                    system_prompt, status, autonomy_level, max_steps, max_run_minutes,
                    max_concurrent_runs, approval_policy_json, metadata_json
                )
                VALUES (
                    $1, $2, $3, $4, '', '', '', 'active', 'supervised',
                    20, 30, 1, '[]'::jsonb, '{}'::jsonb
                )
                """,
                agent_id,
                ids.workspace_one_id,
                name,
                name.lower().replace(" ", "-"),
            )
        await connection.execute(
            """
            INSERT INTO agent_capability_grant
                (id, workspace_id, agent_id, capability, scope_json, effect)
            VALUES ($1, $2, $3, $4, '{}'::jsonb, 'deny')
            """,
            uuid4(),
            ids.workspace_one_id,
            ids.denied_agent_id,
            PERSONA_SELF_CAPABILITY,
        )
    finally:
        await connection.close()


async def _assert_personas_downgraded(dsn: str, ids: MigratedPostgres) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        assert await connection.fetchval("SELECT to_regclass('public.persona')::text") is None
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
        assert "persona_id" not in agent_columns
        remaining = await connection.fetch(
            """
            SELECT agent_id, effect
            FROM agent_capability_grant
            WHERE capability = $1
            """,
            PERSONA_SELF_CAPABILITY,
        )
        # The backfilled allow is gone; the deny somebody made beforehand is not.
        assert [(row["agent_id"], row["effect"]) for row in remaining] == [
            (ids.denied_agent_id, "deny")
        ]
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def migrated_postgres() -> Iterator[MigratedPostgres]:
    database_name = f"jhin_personas_{uuid4().hex}"

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
        fresh_agent_id=uuid4(),
        denied_agent_id=uuid4(),
    )
    try:
        config = alembic_config(sqlalchemy_url)
        command.upgrade(config, "0035")
        asyncio.run(_seed_pre_persona_rows(asyncpg_dsn, ids))
        # 0036 by name rather than "head": the test is about this revision,
        # and a later one must not change what it proves.
        command.upgrade(config, "0036")
        yield ids
        command.downgrade(config, "0035")
        asyncio.run(_assert_personas_downgraded(asyncpg_dsn, ids))
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


async def _fetch(dsn: str, query: str, *args: Any) -> list[Any]:
    connection = await asyncpg.connect(dsn)
    try:
        return list(await connection.fetch(query, *args))
    finally:
        await connection.close()


def test_every_existing_workspace_receives_the_shipped_cast(
    migrated_postgres: MigratedPostgres,
) -> None:
    rows = asyncio.run(
        _fetch(
            migrated_postgres.asyncpg_dsn,
            """
            SELECT workspace_id, name, display_name, source, enabled, version,
                   tags_json::text AS tags, facets_json::text AS facets
            FROM persona
            ORDER BY workspace_id, name
            """,
        )
    )
    by_workspace: dict[UUID, list[Any]] = {}
    for row in rows:
        by_workspace.setdefault(row["workspace_id"], []).append(row)
    assert set(by_workspace) == {
        migrated_postgres.workspace_one_id,
        migrated_postgres.workspace_two_id,
    }
    for workspace_rows in by_workspace.values():
        assert len(workspace_rows) == SHIPPED_CARDS
        assert {row["source"] for row in workspace_rows} == {"built_in"}
        assert all(row["enabled"] for row in workspace_rows)
        assert all(row["version"] >= 1 for row in workspace_rows)
        assert all(row["tags"].startswith("[") for row in workspace_rows)
        assert all('"voice"' in row["facets"] for row in workspace_rows)
        names = [row["name"] for row in workspace_rows]
        assert names == sorted(names)
        assert "the-skeptic" in names
        assert "mission-control" in names


def test_the_agent_column_and_its_foreign_key_exist(
    migrated_postgres: MigratedPostgres,
) -> None:
    columns = asyncio.run(
        _fetch(
            migrated_postgres.asyncpg_dsn,
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'agent' AND column_name = 'persona_id'
            """,
        )
    )
    assert [row["is_nullable"] for row in columns] == ["YES"]
    constraints = asyncio.run(
        _fetch(
            migrated_postgres.asyncpg_dsn,
            """
            SELECT conname, confdeltype::text AS confdeltype
            FROM pg_constraint
            WHERE conname IN ('fk_agent_persona', 'uq_persona_workspace_id_name',
                              'fk_persona_created_by_agent_id_agent')
            ORDER BY conname
            """,
        )
    )
    # confdeltype 'n' is ON DELETE SET NULL; a unique constraint has ' '.
    assert [(row["conname"], row["confdeltype"]) for row in constraints] == [
        ("fk_agent_persona", "n"),
        ("fk_persona_created_by_agent_id_agent", "n"),
        ("uq_persona_workspace_id_name", " "),
    ]


def test_existing_agents_get_the_self_persona_grant_unless_already_decided(
    migrated_postgres: MigratedPostgres,
) -> None:
    rows = asyncio.run(
        _fetch(
            migrated_postgres.asyncpg_dsn,
            """
            SELECT agent_id, effect, scope_json::text AS scope
            FROM agent_capability_grant
            WHERE capability = $1
            ORDER BY agent_id
            """,
            PERSONA_SELF_CAPABILITY,
        )
    )
    by_agent = {row["agent_id"]: (row["effect"], row["scope"]) for row in rows}
    assert by_agent == {
        migrated_postgres.fresh_agent_id: ("allow", "{}"),
        migrated_postgres.denied_agent_id: ("deny", "{}"),
    }
