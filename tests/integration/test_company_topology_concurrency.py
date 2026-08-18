"""PostgreSQL transaction regressions for company-topology mutations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jhin_api.agents import service
from jhin_api.deps import WorkspaceContext
from jhin_db import create_engine, create_session_factory
from jhin_db.migrate import upgrade_to_head
from jhin_db.models import Agent, AgentTeamMembership, Team, User, Workspace
from jhin_domain import WorkspaceRole, new_uuid7

from .conftest import POSTGRES_HOST as PG_HOST
from .conftest import POSTGRES_PORT as PG_PORT

pytestmark = pytest.mark.integration

PG_USER = "jhin"
PG_PASSWORD = "jhin"
ADMIN_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/postgres"


@dataclass(frozen=True)
class PgDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


class RequestMeta(TypedDict):
    request_id: UUID
    ip_hash: str


@pytest.fixture
async def topology_database() -> AsyncIterator[PgDatabase]:
    database_name = f"jhin_topology_{uuid4().hex}"
    database_url = (
        f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{database_name}"
    )
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await admin.close()
    try:
        await asyncio.to_thread(upgrade_to_head, database_url)
        engine = create_engine(database_url)
        yield PgDatabase(engine=engine, sessions=create_session_factory(engine))
        await engine.dispose()
    finally:
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                database_name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        finally:
            await admin.close()


async def _seed_company(
    database: PgDatabase,
) -> tuple[WorkspaceContext, Agent, Agent, list[Team]]:
    async with database.sessions() as session:
        user = User(
            email=f"topology-{new_uuid7().hex[:8]}@example.com",
            display_name="Topology Admin",
            password_hash="x",
        )
        workspace = Workspace(name="Topology", slug=f"topology-{new_uuid7().hex[:8]}")
        session.add_all([user, workspace])
        await session.flush()
        first = Agent(workspace_id=workspace.id, name="First", slug="first")
        second = Agent(workspace_id=workspace.id, name="Second", slug="second")
        teams = [
            Team(workspace_id=workspace.id, name=name)
            for name in ("Primary", "Old Secondary", "New Secondary", "New Primary")
        ]
        session.add_all([first, second, *teams])
        await session.commit()
        return (
            WorkspaceContext(user=user, workspace_id=workspace.id, role=WorkspaceRole.ADMIN),
            first,
            second,
            teams,
        )


async def _backend_pid(session: AsyncSession) -> int:
    pid = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


async def _wait_until_lock_wait(database: PgDatabase, pid: int) -> None:
    for _ in range(200):
        async with database.sessions() as observer:
            wait_type = await observer.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if wait_type == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"backend {pid} did not enter a PostgreSQL lock wait")


def _meta() -> RequestMeta:
    return {"request_id": new_uuid7(), "ip_hash": "postgres-concurrency"}


async def test_concurrent_opposing_manager_updates_cannot_commit_a_cycle(
    topology_database: PgDatabase,
) -> None:
    ctx, first, second, _teams = await _seed_company(topology_database)
    advisory_key = uuid4().int % (2**31)
    function_name = f"block_manager_{uuid4().hex}"
    trigger_name = f"block_manager_trigger_{uuid4().hex}"

    async with topology_database.sessions() as setup:
        await setup.execute(
            text(
                f"""
                CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.manager_agent_id IS DISTINCT FROM OLD.manager_agent_id THEN
                        PERFORM pg_advisory_xact_lock({advisory_key});
                    END IF;
                    RETURN NEW;
                END
                $$
                """
            )
        )
        await setup.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE UPDATE ON agent
                FOR EACH ROW EXECUTE FUNCTION {function_name}()
                """
            )
        )
        await setup.commit()

    async with (
        topology_database.sessions() as blocker,
        topology_database.sessions() as first_session,
        topology_database.sessions() as second_session,
    ):
        await blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": advisory_key})
        first_pid = await _backend_pid(first_session)
        second_pid = await _backend_pid(second_session)
        first_task = asyncio.create_task(
            service.update_agent(
                first_session,
                ctx,
                first.id,
                changes={"manager_agent_id": second.id},
                **_meta(),
            )
        )
        await _wait_until_lock_wait(topology_database, first_pid)
        second_task = asyncio.create_task(
            service.update_agent(
                second_session,
                ctx,
                second.id,
                changes={"manager_agent_id": first.id},
                **_meta(),
            )
        )
        await _wait_until_lock_wait(topology_database, second_pid)
        await blocker.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": advisory_key})
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)
        await first_session.rollback()
        await second_session.rollback()

    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    assert len(conflicts) == 1
    assert "cycle" in str(conflicts[0].detail).lower()
    assert len([result for result in results if isinstance(result, Agent)]) == 1
    async with topology_database.sessions() as verification:
        manager_rows = (
            await verification.execute(
                select(Agent.id, Agent.manager_agent_id).where(
                    Agent.workspace_id == ctx.workspace_id
                )
            )
        ).all()
        managers: dict[UUID, UUID | None] = {row[0]: row[1] for row in manager_rows}
    assert not (managers[first.id] == second.id and managers[second.id] == first.id)


async def test_concurrent_membership_replace_is_seen_by_legacy_team_patch(
    topology_database: PgDatabase,
) -> None:
    ctx, agent, _other, teams = await _seed_company(topology_database)
    primary, old_secondary, new_secondary, new_primary = teams
    async with topology_database.sessions() as setup:
        setup.add_all(
            [
                AgentTeamMembership(
                    workspace_id=ctx.workspace_id,
                    agent_id=agent.id,
                    team_id=primary.id,
                    is_primary=True,
                ),
                AgentTeamMembership(
                    workspace_id=ctx.workspace_id,
                    agent_id=agent.id,
                    team_id=old_secondary.id,
                    is_primary=False,
                ),
            ]
        )
        stored_agent = await setup.get(Agent, agent.id)
        assert stored_agent is not None
        stored_agent.team_id = primary.id
        await setup.commit()

    async with (
        topology_database.sessions() as blocker,
        topology_database.sessions() as replace_session,
        topology_database.sessions() as patch_session,
    ):
        await blocker.execute(select(Agent).where(Agent.id == agent.id).with_for_update())
        replace_pid = await _backend_pid(replace_session)
        patch_pid = await _backend_pid(patch_session)
        replace_task = asyncio.create_task(
            service.replace_memberships(
                replace_session,
                ctx,
                agent.id,
                primary_team_id=primary.id,
                secondary_team_ids=[new_secondary.id],
                **_meta(),
            )
        )
        await _wait_until_lock_wait(topology_database, replace_pid)
        patch_task = asyncio.create_task(
            service.update_agent(
                patch_session,
                ctx,
                agent.id,
                changes={"team_id": new_primary.id},
                **_meta(),
            )
        )
        await _wait_until_lock_wait(topology_database, patch_pid)
        await blocker.commit()
        await asyncio.gather(replace_task, patch_task)

    async with topology_database.sessions() as verification:
        stored_agent = await verification.get(Agent, agent.id)
        assert stored_agent is not None
        active = list(
            await verification.scalars(
                select(AgentTeamMembership).where(
                    AgentTeamMembership.workspace_id == ctx.workspace_id,
                    AgentTeamMembership.agent_id == agent.id,
                    AgentTeamMembership.left_at.is_(None),
                )
            )
        )
    assert stored_agent.team_id == new_primary.id
    assert {(row.team_id, row.is_primary) for row in active} == {
        (new_primary.id, True),
        (new_secondary.id, False),
    }
