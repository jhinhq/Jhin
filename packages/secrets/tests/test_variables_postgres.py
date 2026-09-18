"""Scoped variable transactions against an isolated, fully migrated PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_db.models import Agent, Connection, Conversation, Message, Secret, User, Workspace
from jhin_db.models.variables import ScopedVariable, SecureInputCapture, VariableConnectionBinding
from jhin_domain import new_uuid7
from jhin_secrets import MasterKey, SecretCrypto
from jhin_secrets.intake import capture_input
from jhin_secrets.variables import VariableActor, VariableError, VariableStore

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated TEST_DATABASE_URL required")


@pytest.fixture
async def database():
    engine = create_async_engine(URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        workspace = Workspace(name="Variables test", slug=f"variables-{new_uuid7().hex}")
        user = User(
            email=f"{new_uuid7().hex}@example.test", display_name="Owner", password_hash="x"
        )
        db.add_all([workspace, user])
        await db.flush()
        agent = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        db.add(agent)
        await db.flush()
        chat = Conversation(
            workspace_id=workspace.id,
            primary_agent_id=agent.id,
            created_by_user_id=user.id,
            title="Secure setup",
            last_activity_at=datetime.now(UTC),
        )
        db.add(chat)
        await db.commit()
    crypto = SecretCrypto(MasterKey(key=b"t" * 32))
    actor = VariableActor(workspace.id, "user", user.id, is_admin=True)
    yield factory, actor, agent.id, chat.id, crypto
    async with factory() as db:
        for model in (VariableConnectionBinding, SecureInputCapture, ScopedVariable, Secret):
            await db.execute(delete(model).where(model.workspace_id == workspace.id))
        await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()
    await engine.dispose()


async def test_concurrent_cas_waits_and_rejects_stale_version(database):
    factory, actor, agent_id, _chat, crypto = database
    async with factory() as db:
        row = await VariableStore(db, crypto).set(
            actor, name="tone", scope="agent", scope_id=agent_id, value="initial"
        )
        await db.commit()
        identifier = row.id
    async with factory() as first:
        await VariableStore(first, crypto).set(
            actor, variable_id=identifier, expected_version=1, value="first"
        )

        async def stale_writer():
            async with factory() as second:
                with pytest.raises(VariableError, match="version"):
                    await VariableStore(second, crypto).set(
                        actor, variable_id=identifier, expected_version=1, value="stale"
                    )
                await second.rollback()

        waiting = asyncio.create_task(stale_writer())
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await first.commit()
        await asyncio.wait_for(waiting, 5)
    async with factory() as db:
        row = await VariableStore(db).get(actor, identifier)
        assert row.version == 2 and row.plaintext == "first"


async def test_duplicate_secure_ingress_keeps_one_capture_and_no_journal_plaintext(database):
    factory, actor, agent_id, chat_id, crypto = database
    credential = "12" * 12 + ":" + "ab" * 32

    async def send():
        async with factory() as db:
            await db.scalar(
                select(Workspace.id)
                .where(Workspace.id == actor.workspace_id)
                .with_for_update(key_share=True)
            )
            captured = await capture_input(
                db,
                crypto,
                workspace_id=actor.workspace_id,
                conversation_id=chat_id,
                agent_id=agent_id,
                user_id=actor.actor_id,
                text="Ghost API key: " + credential,
            )
            db.add(
                Message(
                    workspace_id=actor.workspace_id,
                    conversation_id=chat_id,
                    sender_type="user",
                    sender_id=actor.actor_id,
                    recipient_type="agent",
                    recipient_id=agent_id,
                    content_json={"text": captured.text, "secure_inputs": captured.references},
                    visibility="visible",
                )
            )
            await db.commit()
            return captured.references

    first, second = await asyncio.gather(send(), send())
    assert first == second
    async with factory() as db:
        captures = (
            await db.scalars(
                select(SecureInputCapture).where(
                    SecureInputCapture.workspace_id == actor.workspace_id
                )
            )
        ).all()
        assert len(captures) == 1
        for table in (
            "conversation",
            "message",
            "conversation_event",
            "secure_input_capture",
            "secret",
        ):
            rows = await db.scalars(
                text(
                    f"SELECT row_to_json(t)::text FROM {table} t WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": actor.workspace_id},
            )
            assert all(credential not in row for row in rows)
        stored = await db.get(Secret, captures[0].secret_id)
        assert stored.masked_hint == ""


async def test_delete_waits_for_authorized_connection_then_revokes_atomically(database):
    factory, actor, agent_id, _chat, crypto = database
    agent_actor = VariableActor(actor.workspace_id, "agent", agent_id)
    async with factory() as db:
        store = VariableStore(db, crypto)
        variable = await store.set(
            actor,
            name="ghost.key",
            scope="agent",
            scope_id=agent_id,
            sensitive=True,
            value="synthetic-revocation-secret",
        )
        connection = Connection(
            workspace_id=actor.workspace_id,
            name="Ghost",
            connector_type="ghost",
            auth_type="api_key",
            config_json={"admin_url": "https://blog.example.test"},
        )
        db.add(connection)
        await db.flush()
        await store.bind(
            agent_actor,
            variable.id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
        await db.commit()
        variable_id, connection_id = variable.id, connection.id
    async with factory() as executing:
        await executing.scalar(
            select(Connection.id).where(Connection.id == connection_id).with_for_update(read=True)
        )
        await executing.scalar(
            select(ScopedVariable.id)
            .where(ScopedVariable.id == variable_id)
            .with_for_update(read=True)
        )

        async def revoke():
            async with factory() as db:
                await VariableStore(db, crypto).delete(actor, variable_id, expected_version=1)
                await db.commit()

        waiting = asyncio.create_task(revoke())
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await executing.commit()
        await asyncio.wait_for(waiting, 5)
    async with factory() as db:
        assert (await db.get(Connection, connection_id)).status == "disabled"
        assert await db.get(ScopedVariable, variable_id) is None
        with pytest.raises(VariableError):
            await VariableStore(db, crypto).resolve_bound(
                agent_actor,
                variable_id,
                connection_id,
                credential_field="admin_key",
                approved_origin="https://blog.example.test",
            )


async def test_parallel_gateway_consumers_do_not_deadlock_on_secret_usage_stamp(database):
    from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
    from jhin_tools.gateway import ToolGateway

    factory, actor, agent_id, _chat, crypto = database
    caller = VariableActor(actor.workspace_id, "agent", agent_id)
    async with factory() as db:
        store = VariableStore(db, crypto)
        variable = await store.set(
            actor,
            name="parallel.key",
            scope="agent",
            scope_id=agent_id,
            sensitive=True,
            value="synthetic-parallel-consumer-key",
        )
        connection = Connection(
            workspace_id=actor.workspace_id,
            name="Parallel Ghost",
            connector_type="ghost",
            auth_type="api_key",
            config_json={
                "admin_url": "https://blog.example.test",
                "admin_key_variable_id": str(variable.id),
            },
        )
        db.add(connection)
        await db.flush()
        await store.bind(
            caller,
            variable.id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
        await db.commit()
        variable_id, connection_id = variable.id, connection.id
    ready = asyncio.Barrier(2)

    async def consume():
        async with factory() as db:
            ctx = ToolExecutionContext(
                session=db,
                workspace_id=actor.workspace_id,
                task_id=new_uuid7(),
                run_id=new_uuid7(),
                agent_id=agent_id,
                agent_name="Writer",
                crypto=crypto,
            )
            gateway = ToolGateway(ctx, ToolCatalog())
            await ready.wait()
            assert await gateway._connection_authorization_digest(connection_id, lock=True)
            value = await VariableStore(db, crypto).resolve_bound(
                caller,
                variable_id,
                connection_id,
                credential_field="admin_key",
                approved_origin="https://blog.example.test",
            )
            assert value == "synthetic-parallel-consumer-key"
            # Give both SHARE-lock holders time to reach the usage update.
            # With the correct exclusive credential lock, the other consumer
            # instead waits before revealing and can proceed after this commit.
            await asyncio.sleep(0.1)
            await db.flush()
            await db.commit()

    await asyncio.wait_for(asyncio.gather(consume(), consume()), 10)


async def test_ghost_bind_and_delete_use_the_same_workspace_then_connection_lock_order(
    database, monkeypatch
):
    from jhin_connectors.ghost.client import GhostApiError
    from jhin_connectors.ghost.setup import GhostBindInput, bind_ghost
    from jhin_db.models import Task, WorkspaceMembership
    from jhin_secrets.authority import attest_human_content
    from jhin_tools.builtin import ToolExecutionContext

    factory, actor, agent_id, chat_id, crypto = database
    caller = VariableActor(actor.workspace_id, "agent", agent_id)
    async with factory() as db:
        db.add(
            WorkspaceMembership(
                workspace_id=actor.workspace_id, user_id=actor.actor_id, role="owner"
            )
        )
        task = Task(
            workspace_id=actor.workspace_id,
            assigned_agent_id=agent_id,
            conversation_id=chat_id,
            title="Connect Ghost",
            correlation_id=new_uuid7(),
        )
        db.add(task)
        await db.flush()
        db.add(
            Message(
                workspace_id=actor.workspace_id,
                task_id=task.id,
                conversation_id=chat_id,
                sender_type="user",
                sender_id=actor.actor_id,
                recipient_type="agent",
                recipient_id=agent_id,
                content_json=attest_human_content(
                    {"text": "Connect Ghost at https://blog.example.test."},
                    workspace_id=actor.workspace_id,
                    user_id=actor.actor_id,
                    role="owner",
                ),
                visibility="visible",
            )
        )
        store = VariableStore(db, crypto)
        variable = await store.set(
            actor,
            name="race.key",
            scope="agent",
            scope_id=agent_id,
            sensitive=True,
            value="ab" * 12 + ":" + "cd" * 32,
        )
        connection = Connection(
            workspace_id=actor.workspace_id,
            name=f"Ghost · {variable.name} · {str(variable.id)[:8]}",
            connector_type="ghost",
            auth_type="api_key",
            config_json={
                "admin_url": "https://blog.example.test",
                "admin_key_variable_id": str(variable.id),
                "configured_by_agent_id": str(agent_id),
            },
        )
        db.add(connection)
        await db.flush()
        await store.bind(
            caller,
            variable.id,
            connection.id,
            credential_field="admin_key",
            approved_origin="https://blog.example.test",
        )
        await db.commit()
        variable_id, connection_id, task_id = variable.id, connection.id, task.id
    requests = []

    async def request(*args, **kwargs):
        requests.append(True)
        return {"posts": []}

    monkeypatch.setattr("jhin_connectors.ghost.setup.ghost_request", request)
    bind_reached_workspace = asyncio.Event()
    async with factory() as deleting, factory() as binding:
        # Hold the first lock in the actual deletion path while bind starts.
        await VariableStore(deleting, crypto)._lock(actor.workspace_id)
        original_scalar = binding.scalar

        async def observe_workspace_lock(statement, *args, **kwargs):
            tables = {getattr(table, "name", None) for table in statement.get_final_froms()}
            if "workspace" in tables and statement._for_update_arg is not None:
                bind_reached_workspace.set()
            return await original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(binding, "scalar", observe_workspace_lock)

        async def bind():
            ctx = ToolExecutionContext(
                session=binding,
                workspace_id=actor.workspace_id,
                task_id=task_id,
                run_id=new_uuid7(),
                agent_id=agent_id,
                agent_name="Writer",
                crypto=crypto,
            )
            try:
                await bind_ghost(
                    ctx,
                    GhostBindInput(variable_id=variable_id, admin_url="https://blog.example.test"),
                )
            except (GhostApiError, VariableError):
                await binding.rollback()
                return "revoked"
            await binding.commit()
            return "bound"

        pending = asyncio.create_task(bind())
        try:
            await asyncio.wait_for(bind_reached_workspace.wait(), 5)
            assert not pending.done()
            await asyncio.wait_for(
                VariableStore(deleting, crypto).delete(actor, variable_id, expected_version=1), 5
            )
            await deleting.commit()
            assert await asyncio.wait_for(pending, 5) == "revoked"
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert requests == []
    async with factory() as db:
        assert await db.get(ScopedVariable, variable_id) is None
        assert (await db.get(Connection, connection_id)).status == "disabled"
