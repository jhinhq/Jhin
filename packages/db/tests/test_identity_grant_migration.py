"""0041 against a real PostgreSQL: every agent that predates
``organization.identity.self`` receives it, unless somebody already decided
otherwise.

Runs on the dev overlay's database like the personas migration test, and skips
the same way when that database is not reachable. SQLite cannot host the round
trip: the chain uses Postgres-only DDL from 0001 on.

The promise being checked is the one 0032 made for memory and asking, restated
for identity: the backfill is additive, it never overwrites a decision, and
running it twice inserts nothing the second time.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from jhin_db.migrate import alembic_config

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_ADMIN_DATABASE = "postgres"
PG_USER = "jhin"
PG_PASSWORD = "jhin"

IDENTITY_SELF_CAPABILITY = "organization.identity.self"


@dataclass(frozen=True)
class MigratedPostgres:
    asyncpg_dsn: str = field(repr=False)
    workspace_id: UUID
    # An agent with no grants at all, one that already carries a deny for the
    # capability, and one whose allow somebody wrote by hand.
    fresh_agent_id: UUID
    denied_agent_id: UUID
    granted_agent_id: UUID


async def _connect_admin() -> asyncpg.Connection[Any]:
    return await asyncpg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_ADMIN_DATABASE,
    )


async def _seed_pre_identity_rows(dsn: str, ids: MigratedPostgres) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            """
            INSERT INTO workspace (id, name, slug, status, default_timezone, settings_json)
            VALUES ($1, 'Jhin HQ', 'jhin-hq', 'active', 'UTC', '{}'::jsonb)
            """,
            ids.workspace_id,
        )
        for agent_id, name in (
            (ids.fresh_agent_id, "Fresh Agent"),
            (ids.denied_agent_id, "Denied Agent"),
            (ids.granted_agent_id, "Granted Agent"),
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
                ids.workspace_id,
                name,
                name.lower().replace(" ", "-"),
            )
        for agent_id, effect in (
            (ids.denied_agent_id, "deny"),
            (ids.granted_agent_id, "allow"),
        ):
            await connection.execute(
                """
                INSERT INTO agent_capability_grant
                    (id, workspace_id, agent_id, capability, scope_json, effect)
                VALUES ($1, $2, $3, $4, '{"marker": "by hand"}'::jsonb, $5)
                """,
                uuid4(),
                ids.workspace_id,
                agent_id,
                IDENTITY_SELF_CAPABILITY,
                effect,
            )
    finally:
        await connection.close()


async def _assert_identity_grants_downgraded(dsn: str, ids: MigratedPostgres) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        remaining = await connection.fetch(
            """
            SELECT agent_id, effect
            FROM agent_capability_grant
            WHERE capability = $1
            ORDER BY effect
            """,
            IDENTITY_SELF_CAPABILITY,
        )
        # The backfilled unscoped allow is gone. The deny somebody made
        # beforehand is not, and neither is the hand-written allow — its scope
        # is not empty, so the downgrade cannot mistake it for its own work.
        assert [(row["agent_id"], row["effect"]) for row in remaining] == [
            (ids.granted_agent_id, "allow"),
            (ids.denied_agent_id, "deny"),
        ]
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def migrated_postgres() -> Iterator[MigratedPostgres]:
    database_name = f"jhin_identity_{uuid4().hex}"

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
        workspace_id=uuid4(),
        fresh_agent_id=uuid4(),
        denied_agent_id=uuid4(),
        granted_agent_id=uuid4(),
    )
    try:
        config = alembic_config(sqlalchemy_url)
        command.upgrade(config, "0040")
        asyncio.run(_seed_pre_identity_rows(asyncpg_dsn, ids))
        # 0041 by name rather than "head": the test is about this revision,
        # and a later one must not change what it proves.
        command.upgrade(config, "0041")
        yield ids
        command.downgrade(config, "0040")
        asyncio.run(_assert_identity_grants_downgraded(asyncpg_dsn, ids))
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


async def _grants(dsn: str, agent_id: UUID) -> list[Any]:
    connection = await asyncpg.connect(dsn)
    try:
        return list(
            await connection.fetch(
                """
                SELECT capability, effect, scope_json::text AS scope
                FROM agent_capability_grant
                WHERE agent_id = $1 AND capability = $2
                ORDER BY effect
                """,
                agent_id,
                IDENTITY_SELF_CAPABILITY,
            )
        )
    finally:
        await connection.close()


def test_an_agent_that_predates_the_capability_receives_it(
    migrated_postgres: MigratedPostgres,
) -> None:
    """The live failure this fixes belongs to the agents that already exist:
    told "your name is Bisby", the Senior Software Engineer agreed to answer
    to it for one chat because nothing could write the row."""
    rows = asyncio.run(_grants(migrated_postgres.asyncpg_dsn, migrated_postgres.fresh_agent_id))
    assert [(row["effect"], row["scope"]) for row in rows] == [("allow", "{}")]


def test_a_deny_somebody_made_is_never_papered_over(
    migrated_postgres: MigratedPostgres,
) -> None:
    """A deny is a decision. The filter is a NOT EXISTS on the triple in
    either effect, so the migration adds nothing beside it."""
    rows = asyncio.run(_grants(migrated_postgres.asyncpg_dsn, migrated_postgres.denied_agent_id))
    assert [row["effect"] for row in rows] == ["deny"]


def test_an_existing_allow_is_left_exactly_as_its_author_wrote_it(
    migrated_postgres: MigratedPostgres,
) -> None:
    """Including its scope: a second, unscoped row beside a scoped one would
    widen the grant without anybody asking for it."""
    rows = asyncio.run(_grants(migrated_postgres.asyncpg_dsn, migrated_postgres.granted_agent_id))
    assert len(rows) == 1
    assert rows[0]["effect"] == "allow"
    assert "by hand" in rows[0]["scope"]


def test_running_the_upgrade_again_inserts_nothing(
    migrated_postgres: MigratedPostgres,
) -> None:
    """The migration's own selection query, re-run against the database it
    just wrote: it reads the same state it inserts against, so a second run
    inserts nothing — which is what makes it safe to re-run after a partial
    deploy."""
    module = importlib.import_module("jhin_db.alembic.versions.20260906_0041_identity_self_grant")
    assert module.BACKFILLED_CAPABILITIES == (IDENTITY_SELF_CAPABILITY,)

    async def _still_missing() -> list[Any]:
        url = migrated_postgres.asyncpg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(module._agents_without(IDENTITY_SELF_CAPABILITY))
                return list(result.all())
        finally:
            await engine.dispose()

    assert asyncio.run(_still_missing()) == []
