"""Publication safety across real PostgreSQL transactions, fake Ghost transport.

These cases do not simulate process death or reconcile an actual Ghost server.
Only uniquely owned workspace rows are created and removed in the isolated DB.
"""

import asyncio
import json
import os
from dataclasses import replace
from importlib import import_module

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.access import validator_for
from jhin_connectors.ghost.client import GhostApiError
from jhin_connectors.ghost.tools import GHOST_TOOLS
from jhin_db.models import (
    AgentCapabilityGrant,
    AgentRun,
    GhostEditorialReview,
    Task,
    ToolCall,
    Workspace,
)
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolCatalog
from jhin_tools.gateway import ToolGateway

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL TEST_DATABASE_URL required")
cases = import_module("packages.connectors.tests.test_ghost_editorial")
adversarial = import_module("packages.connectors.tests.test_editorial_adversarial_acceptance")
editorial = cases.editorial


@pytest.fixture
async def session():
    engine = create_async_engine(URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = new_uuid7()
    try:
        async with sessions() as db:
            db.info["fixture_workspace_id"] = workspace_id
            yield db
            await db.rollback()
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.fixture
async def workspace(session):
    identity = session.info["fixture_workspace_id"]
    row = Workspace(id=identity, name="Publication fixture", slug=identity.hex)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def publication(editorial):
    ctx, connection, publisher, post, calls = editorial
    publisher.approval_policy_json = []
    grant = AgentCapabilityGrant(
        workspace_id=ctx.workspace_id,
        agent_id=publisher.id,
        capability="ghost.*",
        effect="allow",
        scope_json={"connection_id": str(connection.id)},
    )
    ctx.session.add(grant)
    identities = []
    for index in range(2):
        task = Task(
            workspace_id=ctx.workspace_id,
            title=f"Publish approved article {index}",
            assigned_agent_id=publisher.id,
            correlation_id=new_uuid7(),
        )
        ctx.session.add(task)
        await ctx.session.flush()
        run = AgentRun(workspace_id=ctx.workspace_id, task_id=task.id, agent_id=publisher.id)
        ctx.session.add(run)
        await ctx.session.flush()
        identities.append((task.id, run.id))
    await ctx.session.commit()
    review = await cases.make_review(editorial)
    await adversarial.read_and_decide(editorial, str(review.id), "approved")
    assert review.status == "approved"
    sessions = async_sessionmaker(ctx.session.bind, expire_on_commit=False)
    worker_engines = [
        create_async_engine(
            URL,
            connect_args={
                "server_settings": {
                    "application_name": f"publication-{ctx.workspace_id.hex}-{index}"
                }
            },
        )
        for index in range(2)
    ]
    workers = [async_sessionmaker(engine, expire_on_commit=False) for engine in worker_engines]
    catalog = ToolCatalog()
    for definition, executor in GHOST_TOOLS:
        catalog.register(definition, executor, validator=validator_for(definition.name))

    def context(db, index):
        task_id, run_id = identities[index]
        return replace(
            ctx,
            session=db,
            session_factory=workers[index],
            task_id=task_id,
            run_id=run_id,
            agent_id=publisher.id,
            agent_name=publisher.name,
        )

    arguments = json.dumps({"connection_id": str(connection.id), "review_id": str(review.id)})
    calls.clear()
    try:
        yield sessions, catalog, context, arguments, review.id, grant.id, post, calls
    finally:
        for engine in worker_engines:
            await engine.dispose()


async def test_revoked_publisher_grant_after_approval_prevents_all_provider_calls(publication):
    sessions, catalog, context, arguments, review_id, grant_id, post, calls = publication
    async with sessions() as revoke:
        await revoke.execute(
            delete(AgentCapabilityGrant).where(AgentCapabilityGrant.id == grant_id)
        )
        await revoke.commit()
    async with sessions() as db:
        outcome = await ToolGateway(context(db, 0), catalog).request(
            "ghost.post.publish", arguments, invocation_id=new_uuid7()
        )
        assert outcome.status == "denied"
    async with sessions() as verify:
        review = await verify.get(GhostEditorialReview, review_id)
        assert review.status == "approved" and review.publication_tool_call_id is None
    assert calls == [] and post["status"] == "draft"


async def test_concurrent_publication_attempts_dispatch_at_most_once(publication, monkeypatch):
    sessions, catalog, context, arguments, review_id, _, post, calls = publication
    sending = asyncio.Event()
    release = asyncio.Event()
    running = []

    async def transport(base, key, method, path, *, body=None, params=None):
        calls.append((method, path, body))
        if method == "PUT":
            sending.set()
            await asyncio.wait_for(release.wait(), 20)
            post.update(body["posts"][0])
        return {"posts": [dict(post)]}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", transport)
    try:
        async with (
            context(None, 0).session_factory() as first_db,
            context(None, 1).session_factory() as second_db,
        ):
            second_name = f"publication-{context(second_db, 1).workspace_id.hex}-1"

            async def publish(db, index):
                return await ToolGateway(context(db, index), catalog).request(
                    "ghost.post.publish", arguments, invocation_id=new_uuid7()
                )

            first = asyncio.create_task(publish(first_db, 0))
            running.append(first)
            await asyncio.wait_for(sending.wait(), 15)
            second = asyncio.create_task(publish(second_db, 1))
            running.append(second)
            async with sessions() as observer:
                async with asyncio.timeout(10):
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = :name "
                            "AND cardinality(pg_blocking_pids(pid)) > 0)"
                        ),
                        {"name": second_name},
                    ):
                        assert not second.done(), second.result()
                        # Refresh pg_stat_activity's transaction snapshot.
                        await observer.rollback()
                        await asyncio.sleep(0.01)
            # The second transaction genuinely waits behind the first write,
            # rather than being scheduled only after the first has finished.
            assert sum(method == "PUT" for method, _, _ in calls) == 1
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(first, second), 20)
        assert [outcome.status for outcome in outcomes] == ["executed", "failed"]
        assert outcomes[1].error_code == "ghost_review_not_publishable"
        assert sum(method == "PUT" for method, _, _ in calls) == 1
        async with sessions() as verify:
            review = await verify.get(GhostEditorialReview, review_id)
            assert review.status == "published"
            winner = await verify.get(ToolCall, review.publication_tool_call_id)
            assert winner.status == "completed"
    finally:
        release.set()
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


async def test_lost_publish_response_stays_uncertain_across_new_gateway_sessions(
    publication, monkeypatch
):
    sessions, catalog, context, arguments, review_id, _, post, calls = publication

    async def transport(base, key, method, path, *, body=None, params=None):
        calls.append((method, path, body))
        if method == "PUT":
            # The remote state changed, but the caller never received proof.
            post.update(body["posts"][0])
            raise GhostApiError("Response lost after dispatch", mutation=True)
        return {"posts": [dict(post)]}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", transport)
    invocation_id = new_uuid7()
    async with sessions() as db:
        first = await ToolGateway(context(db, 0), catalog).request(
            "ghost.post.publish", arguments, invocation_id=invocation_id
        )
        assert first.status == "execution_unknown"
    async with sessions() as verify:
        review = await verify.get(GhostEditorialReview, review_id)
        assert review.status == "uncertain" and review.publication_tool_call_id == invocation_id
    for invocation in (invocation_id, new_uuid7()):
        async with sessions() as fresh_worker:
            outcome = await ToolGateway(context(fresh_worker, 1), catalog).request(
                "ghost.post.publish", arguments, invocation_id=invocation
            )
            assert outcome.status != "executed"
    async with sessions() as verify:
        review = await verify.get(GhostEditorialReview, review_id)
        assert review.status == "uncertain" and review.published_at is None
        assert (
            await verify.scalar(select(ToolCall.status).where(ToolCall.id == invocation_id))
            == "execution_unknown"
        )
    assert post["status"] == "published"
    assert sum(method == "PUT" for method, _, _ in calls) == 1
