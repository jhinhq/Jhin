"""Verify the readiness upgrade in an explicitly named EMPTY migration-test database."""

import asyncio
import os
from uuid import UUID

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection as SQLConnection
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jhin_db.migrate import alembic_config
from jhin_db.models import Agent, AgentCapabilityGrant, Workspace

URL = ""


def migration_head(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "Migration graph has no head"
    return head


async def verify_database_head(db: AsyncConnection | AsyncSession, expected_revision: str) -> None:
    revisions = list((await db.execute(text("SELECT version_num FROM alembic_version"))).scalars())
    assert revisions == [expected_revision], (
        f"Expected migration head {expected_revision}; database revisions are {revisions}"
    )


async def verify_empty() -> None:
    engine = create_async_engine(URL)
    async with engine.connect() as c:
        database_name = (await c.execute(text("select current_database()"))).scalar()
        assert database_name and "migration" in database_name and database_name != "agentic_test"
        tables = await c.run_sync(lambda s: inspect(s).get_table_names())
        assert not tables, tables
    await engine.dispose()


async def seed_pins(ws: UUID, agent_id: UUID) -> dict[str, str]:
    from jhin_db.models import Connection, ScopedVariable
    from jhin_domain import new_uuid7

    engine = create_async_engine(URL)
    examples = [
        (
            "https://EXAMPLE.com:443/blog/ghost/api/admin/",
            "https://example.com",
            "https://example.com/blog",
        ),
        (
            "http://localhost:2368/team/ghost/",
            "http://localhost:2368",
            "http://localhost:2368/team",
        ),
        ("https://example.com/other", "https://different.example", ""),
        ("https://example.com/blog/../other", "https://example.com", ""),
        ("https://user:password@example.com/blog", "https://example.com", ""),
        ("https://example.com/blog?redirect=elsewhere", "https://example.com", ""),
    ]
    expected = {}
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        variable = ScopedVariable(
            workspace_id=ws,
            scope="agent",
            scope_id=agent_id,
            name="migration-fixture",
            plaintext="not-a-credential",
            sensitive=False,
            created_by_type="agent",
            created_by_id=agent_id,
            updated_by_type="agent",
            updated_by_id=agent_id,
        )
        db.add(variable)
        await db.flush()
        for index, (url, origin, pin) in enumerate(examples):
            connection = Connection(
                workspace_id=ws,
                connector_type="ghost",
                name=f"pin-{index}",
                auth_type="api_key",
                config_json={"admin_url": url},
            )
            db.add(connection)
            await db.flush()
            binding_id = new_uuid7()
            await db.execute(
                text(
                    "INSERT INTO variable_connection_binding "
                    "(id, workspace_id, variable_id, connection_id, credential_field, "
                    "approved_origin, created_by_agent_id) "
                    "VALUES (:id,:ws,:variable,:connection,:field,:origin,:agent)"
                ),
                {
                    "id": binding_id,
                    "ws": ws,
                    "variable": variable.id,
                    "connection": connection.id,
                    "field": "admin_api_key",
                    "origin": origin,
                    "agent": agent_id,
                },
            )
            expected[str(binding_id)] = pin
        await db.commit()
    await engine.dispose()
    return expected


async def verify_pins(expected: dict[str, str]) -> None:
    engine = create_async_engine(URL)
    async with engine.connect() as db:
        actual = {
            str(row.id): row.approved_admin_url
            for row in await db.execute(
                text("SELECT id,approved_admin_url FROM variable_connection_binding")
            )
        }
        assert actual == expected, actual
    await engine.dispose()


async def clear_pin_fixtures(ws: UUID) -> None:
    engine = create_async_engine(URL)
    async with engine.begin() as db:
        await db.execute(
            text("DELETE FROM variable_connection_binding WHERE workspace_id=:ws"), {"ws": ws}
        )
        await db.execute(text("DELETE FROM scoped_variable WHERE workspace_id=:ws"), {"ws": ws})
    await engine.dispose()


async def seed() -> tuple[UUID, list[UUID]]:
    engine = create_async_engine(URL)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        ws = Workspace(name="Readiness migration fixture", slug="readiness-migration-test")
        db.add(ws)
        await db.flush()
        agents = [
            Agent(workspace_id=ws.id, name=n, slug=n.lower()) for n in ("Deny", "Custom", "Fresh")
        ]
        db.add_all(agents)
        await db.flush()
        db.add_all(
            [
                AgentCapabilityGrant(
                    workspace_id=ws.id,
                    agent_id=agents[0].id,
                    capability="schedules.manage",
                    scope_json={},
                    effect="deny",
                ),
                AgentCapabilityGrant(
                    workspace_id=ws.id,
                    agent_id=agents[1].id,
                    capability="ghost.post.read",
                    scope_json={"connection_ids": ["explicit-only"]},
                    effect="allow",
                ),
            ]
        )
        await db.commit()
        result = ws.id, [a.id for a in agents]
    await engine.dispose()
    return result


async def seed_durable_work(ws: UUID, agent_id: UUID, kind: str) -> None:
    from datetime import UTC, datetime, timedelta

    from jhin_db.models import AgentSchedule, ScheduleOccurrence, UserQuestion

    engine = create_async_engine(URL)
    now = datetime.now(UTC)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        if kind in {"schedule", "occurrence"}:
            schedule = AgentSchedule(
                workspace_id=ws,
                agent_id=agent_id,
                name="Retained fixture",
                brief="A retired schedule still preserves its history.",
                local_time="09:00",
                timezone="UTC",
                enabled=False,
                deleted_at=now,
                idempotency_key=f"migration-{kind}",
                request_hash="0" * 64,
            )
            db.add(schedule)
            await db.flush()
            if kind == "occurrence":
                db.add(
                    ScheduleOccurrence(
                        workspace_id=ws,
                        schedule_id=schedule.id,
                        scheduled_for=now,
                        status="completed",
                        finished_at=now,
                    )
                )
        else:
            db.add(
                UserQuestion(
                    workspace_id=ws,
                    agent_id=agent_id,
                    question="Confirmed destination?",
                    required=True,
                    input_key="destination_url",
                    value_type="url",
                    status="answered",
                    answer_kind="other",
                    answer_text="https://example.invalid/blog",
                    asked_at=now,
                    answered_at=now,
                    expires_at=now + timedelta(minutes=30),
                    dedupe_hash="1" * 64,
                    idempotency_key="migration-question",
                )
            )
        await db.commit()
    await engine.dispose()


async def check_and_clear_durable_fixture(ws: UUID, kind: str, expected_revision: str) -> None:
    engine = create_async_engine(URL)
    table = {
        "schedule": "agent_schedule",
        "occurrence": "schedule_occurrence",
        "question": "user_question",
    }[kind]
    async with engine.begin() as db:
        await verify_database_head(db, expected_revision)
        assert (
            await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id=:ws"), {"ws": ws}
            )
        ).scalar() == 1
        for fixture_table in ("schedule_occurrence", "agent_schedule", "user_question"):
            await db.execute(
                text(f"DELETE FROM {fixture_table} WHERE workspace_id=:ws"), {"ws": ws}
            )
    await engine.dispose()


async def verify(ws: UUID, agents: list[UUID], expected_revision: str) -> None:
    engine = create_async_engine(URL)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await verify_database_head(db, expected_revision)
        denied = list(
            await db.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == agents[0],
                    AgentCapabilityGrant.capability == "schedules.manage",
                )
            )
        )
        assert len(denied) == 1 and denied[0].effect == "deny"
        custom = list(
            await db.scalars(
                select(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.agent_id == agents[1],
                    AgentCapabilityGrant.capability == "ghost.post.read",
                )
            )
        )
        assert len(custom) == 1 and custom[0].scope_json == {"connection_ids": ["explicit-only"]}
        fresh = list(
            await db.scalars(
                select(AgentCapabilityGrant).where(AgentCapabilityGrant.agent_id == agents[2])
            )
        )
        assert {
            "schedules.manage",
            "schedules.read",
            "variables.read",
            "variables.write",
            "ghost.connection.bind",
        } <= {g.capability for g in fresh}
        ghost = next(g for g in fresh if g.capability == "ghost.post.publish")
        assert ghost.scope_json == {"variable_audience": True}
        async with engine.connect() as c:
            for name in (
                "agent_schedule",
                "schedule_occurrence",
                "scoped_variable",
                "secure_input_capture",
                "ghost_editorial_review",
            ):

                def has_table(connection: SQLConnection, table_name: str) -> bool:
                    return inspect(connection).has_table(table_name)

                assert await c.run_sync(has_table, name)
            columns = await c.run_sync(
                lambda s: {v["name"] for v in inspect(s).get_columns("scoped_variable")}
            )
            assert {"source_variable_id", "source_version"} <= columns
    await engine.dispose()


def main() -> None:
    global URL
    URL = os.environ["MIGRATION_TEST_DATABASE_URL"]
    config = alembic_config(URL)
    expected_revision = migration_head(config)
    asyncio.run(verify_empty())
    command.upgrade(config, "head")
    print(f"PASS clean base -> {expected_revision}")
    command.downgrade(config, "0047")
    print(f"PASS empty {expected_revision} -> 0047")
    ws, agents = asyncio.run(seed())
    command.upgrade(config, "0050")
    expected = asyncio.run(seed_pins(ws, agents[2]))
    command.upgrade(config, "head")
    asyncio.run(verify(ws, agents, expected_revision))
    asyncio.run(verify_pins(expected))
    print(
        f"PASS 0047 -> {expected_revision}: grants preserved; subdirectory pins normalized; "
        "mismatched/malformed origins fail closed"
    )
    try:
        command.downgrade(config, "0047")
    except RuntimeError as exc:
        assert "Remove variable app bindings" in str(exc)
    else:
        raise AssertionError("Unsafe downgrade accepted populated bindings")
    print("PASS populated binding downgrade refused")
    asyncio.run(clear_pin_fixtures(ws))
    for kind in ("schedule", "occurrence", "question"):
        asyncio.run(seed_durable_work(ws, agents[2], kind))
        try:
            command.downgrade(config, "0047")
        except RuntimeError as exc:
            assert "Cannot downgrade while" in str(exc)
            assert "schedule" in str(exc) or "question" in str(exc)
        else:
            raise AssertionError(f"Unsafe downgrade accepted retained {kind}")
        asyncio.run(check_and_clear_durable_fixture(ws, kind, expected_revision))
        print(f"PASS retained {kind} downgrade refused; head and fixture unchanged")
    command.downgrade(config, "0047")
    command.upgrade(config, "head")
    asyncio.run(verify(ws, agents, expected_revision))
    print(
        f"PASS explicit fixture cleanup then 0047 -> {expected_revision}; "
        "no duplicate or broadened grants"
    )


if __name__ == "__main__":
    main()
