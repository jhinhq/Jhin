"""Real PostgreSQL locks protect cross-connection claims and queued draft writes."""

import asyncio
import os

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.authority import installation_authority
from jhin_connectors.ghost.client import GhostApiError
from jhin_connectors.ghost.schemas import DraftCreateInput
from jhin_connectors.ghost.tools import _draft_create
from jhin_db.models import Agent, Connection, Workspace
from jhin_db.models.editorial import EditorialAssignment, GhostInstallation
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL TEST_DATABASE_URL required")


async def wait_for_real_lock(engine, application_name):
    async with engine.connect() as observer:
        async with asyncio.timeout(10):
            while not await observer.scalar(  # noqa: ASYNC110 - observe an external PostgreSQL lock
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name=:name "
                    "AND cardinality(pg_blocking_pids(pid)) > 0)"
                ),
                {"name": application_name},
            ):
                await asyncio.sleep(0.02)


async def test_installation_publisher_claims_serialize_across_connections():
    engine = create_async_engine(URL)
    name = f"editorial-authority-{new_uuid7().hex}"
    contender_engine = create_async_engine(
        URL, connect_args={"server_settings": {"application_name": name}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    contenders = async_sessionmaker(contender_engine, expire_on_commit=False)
    workspace = Workspace(name="Editorial race fixture", slug=new_uuid7().hex)
    second = None
    try:
        async with sessions() as db:
            db.add(workspace)
            await db.flush()
            first_agent = Agent(workspace_id=workspace.id, name="Ashley", slug="ashley")
            second_agent = Agent(workspace_id=workspace.id, name="Other", slug="other")
            db.add_all([first_agent, second_agent])
            await db.commit()
        async with sessions() as first:
            await installation_authority(
                first,
                workspace.id,
                "https://blog.example.test/ghost",
                first_agent.id,
                establish=True,
            )

            async def contend():
                async with contenders() as db:
                    await installation_authority(
                        db,
                        workspace.id,
                        "https://blog.example.test",
                        second_agent.id,
                        establish=True,
                    )
                    await db.commit()

            second = asyncio.create_task(contend())
            await wait_for_real_lock(engine, name)
            await first.commit()
        with pytest.raises(GhostApiError, match="conflict"):
            await second
        async with sessions() as db:
            stored = list(
                await db.scalars(
                    select(GhostInstallation).where(GhostInstallation.workspace_id == workspace.id)
                )
            )
            assert len(stored) == 1 and stored[0].publisher_agent_id == first_agent.id
    finally:
        if second and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        async with sessions() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()
        await contender_engine.dispose()
        await engine.dispose()


async def test_cancellation_wins_before_waiting_draft_write_dispatch(monkeypatch):
    engine = create_async_engine(URL)
    name = f"editorial-cancel-{new_uuid7().hex}"
    contender_engine = create_async_engine(
        URL, connect_args={"server_settings": {"application_name": name}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    contenders = async_sessionmaker(contender_engine, expire_on_commit=False)
    workspace = Workspace(name="Editorial cancellation fixture", slug=new_uuid7().hex)
    outgoing = []
    second = None

    async def provider(*args, **kwargs):
        outgoing.append(args)
        raise AssertionError("Cancelled assignment reached provider")

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", provider)
    try:
        async with sessions() as db:
            db.add(workspace)
            await db.flush()
            writer = Agent(workspace_id=workspace.id, name="Mindy", slug="mindy")
            publisher = Agent(workspace_id=workspace.id, name="Ashley", slug="ashley")
            db.add_all([writer, publisher])
            await db.flush()
            connection = Connection(
                workspace_id=workspace.id,
                name="Ghost fixture",
                connector_type="ghost",
                auth_type="api_key",
                config_json={
                    "admin_url": "https://blog.example.test",
                    "publisher_agent_id": str(publisher.id),
                },
            )
            db.add(connection)
            await db.flush()
            assignment = EditorialAssignment(
                workspace_id=workspace.id,
                connection_id=connection.id,
                writer_agent_id=writer.id,
                publisher_agent_id=publisher.id,
            )
            db.add(assignment)
            await db.commit()
        async with sessions() as cancelling:
            locked = await cancelling.scalar(
                select(EditorialAssignment)
                .where(EditorialAssignment.id == assignment.id)
                .with_for_update()
            )
            locked.phase = "cancelled"
            await cancelling.flush()

            async def queued_write():
                async with contenders() as db:
                    ctx = ToolExecutionContext(
                        session=db,
                        workspace_id=workspace.id,
                        agent_id=writer.id,
                        agent_name=writer.name,
                        task_id=new_uuid7(),
                        run_id=new_uuid7(),
                    )
                    return await _draft_create(
                        ctx,
                        DraftCreateInput(
                            connection_id=str(connection.id),
                            assignment_id=str(assignment.id),
                            expected_editorial_version=1,
                            title="Cancelled",
                            slug="cancelled",
                            html="<p>Do not write</p>",
                        ),
                    )

            second = asyncio.create_task(queued_write())
            await wait_for_real_lock(engine, name)
            await cancelling.commit()
        with pytest.raises(GhostApiError, match="cancelled"):
            await second
        assert outgoing == []
    finally:
        if second and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        async with sessions() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()
        await contender_engine.dispose()
        await engine.dispose()
