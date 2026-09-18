"""Approved Ghost writes serialize without connection/credential lock upgrades.

Uses only fixture-owned rows in TEST_DATABASE_URL and a fake Ghost transport.
The second resolver must actually wait on a PostgreSQL lock before the first
continues, making the former SHARE/UPDATE deadlock reproducible rather than
depending on concurrent tasks happening to overlap.
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.access import validator_for
from jhin_connectors.ghost.client import post_revision
from jhin_connectors.ghost.tools import GHOST_TOOLS
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    Connection,
    GhostEditorialReview,
    Task,
    ToolCall,
    User,
    WorkRequest,
    Workspace,
)
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7
from jhin_secrets import MasterKey, SecretCrypto, SecretStore
from jhin_secrets.variables import VariableActor, VariableStore
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL TEST_DATABASE_URL required")
KEY = "de" * 12 + ":" + "ef" * 32


@pytest.mark.parametrize("first_tool", ["ghost.draft.create", "ghost.review.request"])
async def test_approved_create_and_review_wait_then_complete_once(monkeypatch, first_tool):
    workspace_id = new_uuid7()
    application_names = [f"ghost-lock-{workspace_id.hex}-{side}" for side in ("a", "b")]
    engine = create_async_engine(URL)
    engines = [
        create_async_engine(URL, connect_args={"server_settings": {"application_name": name}})
        for name in application_names
    ]
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workers = [async_sessionmaker(item, expire_on_commit=False) for item in engines]
    crypto = SecretCrypto(MasterKey(key=os.urandom(32)))
    release = asyncio.Event()
    first_revealed = asyncio.Event()
    running = []
    transport_calls = []
    post = {
        "id": "c" * 24,
        "title": "Existing review fixture",
        "slug": "existing-review-fixture",
        "html": "<p>Fixture evidence.</p>",
        "lexical": "{}",
        "status": "draft",
        "updated_at": "2031-01-01T09:00:00.000Z",
    }
    created_posts = {}

    async def transport(base, key, method, path, *, body=None, params=None):
        assert base == "https://ghost.example.test" and key == KEY
        transport_calls.append((method, path))
        if method == "GET" and path == "posts/":
            return {"posts": []}
        if method == "POST":
            created = {**post, **body["posts"][0], "id": "d" * 24}
            created_posts["posts/" + created["id"] + "/"] = created
            return {"posts": [created]}
        if method == "GET" and path in created_posts:
            return {"posts": [created_posts[path]]}
        assert method == "GET" and path == f"posts/{post['id']}/"
        return {"posts": [dict(post)]}

    monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", transport)
    original_reveal = SecretStore.reveal

    async def hold_first_credential(store, requested_workspace, secret_id):
        result = await original_reveal(store, requested_workspace, secret_id)
        if (
            asyncio.current_task().get_name() == application_names[0]
            and not first_revealed.is_set()
        ):
            first_revealed.set()
            await asyncio.wait_for(release.wait(), 15)
        return result

    monkeypatch.setattr(SecretStore, "reveal", hold_first_credential)
    catalog = ToolCatalog()
    for definition, executor in GHOST_TOOLS:
        catalog.register(definition, executor, validator=validator_for(definition.name))

    try:
        async with sessions() as db:
            db.add(Workspace(id=workspace_id, name="Ghost lock fixture", slug=workspace_id.hex))
            await db.flush()
            owner = User(
                email=f"ghost-{workspace_id.hex}@example.test",
                display_name="Fixture owner",
                password_hash="fixture",
            )
            writer = Agent(
                workspace_id=workspace_id,
                name="Blogger",
                slug="blogger",
                approval_policy_json=[{"capability": "ghost.*", "action": "approval"}],
            )
            director = Agent(workspace_id=workspace_id, name="Director", slug="director")
            db.add_all([owner, writer, director])
            await db.flush()
            owner_id, writer_id = owner.id, writer.id
            store = VariableStore(db, crypto)
            variable = await store.set(
                VariableActor(workspace_id, "user", owner.id, is_admin=True),
                scope="company",
                scope_id=workspace_id,
                name="ghost.fixture",
                sensitive=True,
                value=KEY,
            )
            connection = Connection(
                workspace_id=workspace_id,
                connector_type="ghost",
                name="Ghost fixture",
                auth_type="api_key",
                config_json={
                    "admin_url": "https://ghost.example.test",
                    "admin_key_variable_id": str(variable.id),
                    "publisher_agent_id": str(director.id),
                },
            )
            db.add(connection)
            await db.flush()
            connection_id = connection.id
            await store.bind(
                VariableActor(workspace_id, "agent", writer.id),
                variable.id,
                connection.id,
                credential_field="admin_key",
                approved_origin="https://ghost.example.test",
            )
            db.add(
                AgentCapabilityGrant(
                    workspace_id=workspace_id,
                    agent_id=writer.id,
                    capability="ghost.*",
                    effect="allow",
                    scope_json={"variable_audience": True},
                )
            )
            identities = []
            for index in range(2):
                task = Task(
                    workspace_id=workspace_id,
                    title=f"Ghost operation {index}",
                    assigned_agent_id=writer.id,
                    correlation_id=new_uuid7(),
                )
                db.add(task)
                await db.flush()
                run = AgentRun(workspace_id=workspace_id, task_id=task.id, agent_id=writer.id)
                db.add(run)
                await db.flush()
                identities.append((task.id, run.id))
            create_assignment = EditorialAssignment(
                workspace_id=workspace_id,
                connection_id=connection.id,
                writer_agent_id=writer.id,
                publisher_agent_id=director.id,
            )
            review_assignment = EditorialAssignment(
                workspace_id=workspace_id,
                connection_id=connection.id,
                writer_agent_id=writer.id,
                publisher_agent_id=director.id,
                post_id=post["id"],
            )
            db.add_all([create_assignment, review_assignment])
            await db.flush()
            assignment_ids = {
                "ghost.draft.create": str(create_assignment.id),
                "ghost.review.request": str(review_assignment.id),
            }
            await db.commit()

        def context(db, index):
            task_id, run_id = identities[index]
            return ToolExecutionContext(
                session=db,
                session_factory=workers[index],
                workspace_id=workspace_id,
                agent_id=writer_id,
                agent_name="Blogger",
                task_id=task_id,
                run_id=run_id,
                crypto=crypto,
            )

        arguments = {
            "ghost.draft.create": {
                "assignment_id": assignment_ids["ghost.draft.create"],
                "expected_editorial_version": 1,
                "connection_id": str(connection_id),
                "title": "Created fixture",
                "slug": "created-fixture",
                "html": "<p>A draft, never a publication.</p>",
            },
            "ghost.review.request": {
                "assignment_id": assignment_ids["ghost.review.request"],
                "expected_editorial_version": 1,
                "connection_id": str(connection_id),
                "post_id": post["id"],
                "expected_revision": post_revision(post),
                "summary": "Review this exact fixture revision.",
            },
        }
        order = [first_tool, next(name for name in arguments if name != first_tool)]
        approvals = []
        for index, tool_name in enumerate(order):
            async with workers[index]() as db:
                parked = await ToolGateway(context(db, index), catalog).request(
                    tool_name, json.dumps(arguments[tool_name]), invocation_id=new_uuid7()
                )
                assert parked.status == "needs_approval", parked.decision_reason
                approval = await db.get(Approval, parked.approval_id)
                approval.status = "approved"
                approval.decided_by_user_id = owner_id
                approval.decided_at = datetime.now(UTC)
                approvals.append(approval.id)
                await db.commit()
        assert transport_calls == []

        async def resolve(index):
            async with workers[index]() as db:
                return await ToolGateway(context(db, index), catalog).resolve_approved(
                    approvals[index]
                )

        running.append(asyncio.create_task(resolve(0), name=application_names[0]))
        await asyncio.wait_for(first_revealed.wait(), 10)
        running.append(asyncio.create_task(resolve(1), name=application_names[1]))
        async with asyncio.timeout(10):
            while True:
                async with engine.connect() as probe:
                    blocked = await probe.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name=:name AND wait_event_type='Lock')"
                        ),
                        {"name": application_names[1]},
                    )
                if blocked:
                    break
                await asyncio.sleep(0.02)
        assert transport_calls == [], "Provider work escaped the intended ordering barrier"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*running), 15)
        assert [result.status for result in results] == ["executed", "executed"]
        assert transport_calls.count(("POST", "posts/")) == 1
        assert all(method != "PUT" for method, _ in transport_calls)
        before_replay = list(transport_calls)
        for index in range(2):
            replay = await resolve(index)
            assert replay.replayed and replay.status == "executed"
        assert transport_calls == before_replay
        async with sessions() as db:
            assert set(
                await db.scalars(
                    select(ToolCall.status).where(ToolCall.workspace_id == workspace_id)
                )
            ) == {"completed"}
            for model in (GhostEditorialReview, WorkRequest):
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == workspace_id)
                    )
                    == 1
                )
    finally:
        release.set()
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        for worker_engine in engines:
            await worker_engine.dispose()
        async with sessions() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db.execute(
                delete(User).where(User.email == f"ghost-{workspace_id.hex}@example.test")
            )
            await db.commit()
        await engine.dispose()
