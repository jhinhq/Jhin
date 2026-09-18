"""Real transaction/row-lock coverage; only Unsplash HTTP is replaced."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.unsplash import tools
from jhin_connectors.unsplash.client import API_ORIGIN, UnsplashError
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    Approval,
    Connection,
    Conversation,
    Task,
    User,
    UserQuestion,
    Workspace,
    WorkspaceMembership,
)
from jhin_db.models.editorial import EditorialAssignment
from jhin_db.models.editorial_assets import EditorialAsset
from jhin_db.models.variables import VariableConnectionBinding
from jhin_domain import new_uuid7
from jhin_secrets import MasterKey, SecretCrypto
from jhin_secrets.variables import VariableActor, VariableStore
from jhin_tools.builtin import ToolCatalog, ToolExecutionContext
from jhin_tools.gateway import ToolGateway

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated PostgreSQL TEST_DATABASE_URL required")


def photo():
    return {
        "id": "photoABC",
        "width": 1200,
        "height": 800,
        "urls": {"regular": "https://images.unsplash.com/example?ixid=fixture"},
        "links": {
            "html": "https://unsplash.com/photos/photoABC",
            "download_location": f"{API_ORIGIN}/photos/photoABC/download?ixid=fixture",
        },
        "user": {"name": "Fixture", "links": {"html": "https://unsplash.com/@fixture"}},
    }


@asynccontextmanager
async def selection_database():
    engine = create_async_engine(URL, connect_args={"server_settings": {"lock_timeout": "2000"}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    crypto = SecretCrypto(MasterKey(key=os.urandom(32)))
    workspace = Workspace(name="Unsplash transaction fixture", slug=new_uuid7().hex)
    owner = User(
        email=f"{new_uuid7().hex}@example.test", display_name="Owner", password_hash="fixture"
    )
    try:
        async with sessions() as db:
            db.add_all([workspace, owner])
            await db.flush()
            writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
            db.add(writer)
            await db.flush()
            conversation = Conversation(
                workspace_id=workspace.id,
                primary_agent_id=writer.id,
                created_by_user_id=owner.id,
                title="Images",
                last_activity_at=datetime.now(UTC),
            )
            db.add_all(
                [
                    conversation,
                    WorkspaceMembership(workspace_id=workspace.id, user_id=owner.id, role="owner"),
                ]
            )
            await db.flush()
            task = Task(
                workspace_id=workspace.id,
                title="Select image",
                assigned_agent_id=writer.id,
                conversation_id=conversation.id,
                correlation_id=new_uuid7(),
            )
            db.add(task)
            await db.flush()
            run = AgentRun(workspace_id=workspace.id, task_id=task.id, agent_id=writer.id)
            db.add(run)
            store = VariableStore(db, crypto)
            variable = await store.set(
                VariableActor(workspace.id, "user", owner.id, is_admin=True),
                name="unsplash.fixture",
                scope="company",
                scope_id=workspace.id,
                sensitive=True,
                value="synthetic-access-key",
            )
            connection = Connection(
                workspace_id=workspace.id,
                name="Unsplash fixture",
                connector_type="unsplash",
                auth_type="api_key",
                config_json={"admin_url": API_ORIGIN, "access_key_variable_id": str(variable.id)},
            )
            ghost = Connection(
                workspace_id=workspace.id,
                name="Ghost fixture",
                connector_type="ghost",
                auth_type="api_key",
                config_json={"admin_url": "https://ghost.example.test"},
            )
            db.add_all([connection, ghost])
            await db.flush()
            await store.bind(
                VariableActor(workspace.id, "agent", writer.id),
                variable.id,
                connection.id,
                credential_field="access_key",
                approved_origin=API_ORIGIN,
            )
            assignment = EditorialAssignment(
                workspace_id=workspace.id,
                connection_id=ghost.id,
                writer_agent_id=writer.id,
                publisher_agent_id=writer.id,
                conversation_id=conversation.id,
                task_id=task.id,
            )
            question = UserQuestion(
                workspace_id=workspace.id,
                conversation_id=conversation.id,
                task_id=task.id,
                agent_id=writer.id,
                kind="open",
                input_key="unsplash_photo",
                question="Choose a photo",
                dedupe_hash="f" * 64,
                idempotency_key="photo",
                status="answered",
                asked_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                answered_at=datetime.now(UTC),
                answered_by_user_id=owner.id,
                answer_kind="option",
                answer_option_value="photoABC",
            )
            db.add_all(
                [
                    assignment,
                    question,
                    AgentCapabilityGrant(
                        workspace_id=workspace.id,
                        agent_id=writer.id,
                        capability="unsplash.*",
                        effect="allow",
                        scope_json={"variable_audience": True},
                    ),
                ]
            )
            await db.commit()

        def context(db):
            return ToolExecutionContext(
                session=db,
                session_factory=sessions,
                workspace_id=workspace.id,
                agent_id=writer.id,
                agent_name="Writer",
                task_id=task.id,
                run_id=run.id,
                crypto=crypto,
            )

        payload = tools.SelectInput(
            connection_id=connection.id, assignment_id=assignment.id, question_id=question.id
        )
        yield sessions, context, payload, variable, owner
    finally:
        async with sessions() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()
        await engine.dispose()


@pytest.fixture
async def selection_pg():
    async with selection_database() as selection:
        yield selection


async def test_parallel_workers_reserve_one_selection_and_increment_once(selection_pg, monkeypatch):
    sessions, context, payload, *_ = selection_pg
    metadata_ready = asyncio.Event()
    arrivals = 0
    calls = []

    async def request(key, path, params=None):
        nonlocal arrivals
        calls.append(path)
        if path.endswith("/download"):
            return {"url": "https://images.unsplash.com/example"}
        arrivals += 1
        if arrivals == 2:
            metadata_ready.set()
        await asyncio.wait_for(metadata_ready.wait(), 10)
        return photo()

    monkeypatch.setattr(tools, "request", request)

    async def worker():
        async with sessions() as db:
            return await tools.select_photo(context(db), payload)

    results = await asyncio.wait_for(asyncio.gather(worker(), worker(), return_exceptions=True), 15)
    successes = [result for result in results if isinstance(result, tools.Result)]
    assert len(successes) == 1, results
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], UnsplashError), results
    assert errors[0].code == "unsplash_tracking_uncertain"
    assert calls.count("/photos/photoABC/download") == 1
    async with sessions() as db:
        asset = (
            await db.scalars(
                select(EditorialAsset).where(EditorialAsset.assignment_id == payload.assignment_id)
            )
        ).one()
        assert asset.status == "confirmed" and asset.tracking_confirmed_at is not None
        assignment = await db.get(EditorialAssignment, payload.assignment_id)
        assert assignment.editorial_version == 2 and assignment.version == 2
        replay = await tools.select_photo(context(db), payload)
        assert replay.data == successes[0].data
    assert calls.count("/photos/photoABC/download") == 1


async def test_credential_binding_revocation_during_metadata_blocks_tracking(
    selection_pg, monkeypatch
):
    sessions, context, payload, *_ = selection_pg
    calls = []

    async def request(key, path, params=None):
        calls.append(path)
        async with sessions() as revoke:
            await revoke.execute(
                delete(VariableConnectionBinding).where(
                    VariableConnectionBinding.connection_id == payload.connection_id
                )
            )
            await revoke.commit()
        return photo()

    monkeypatch.setattr(tools, "request", request)
    async with sessions() as db:
        with pytest.raises(UnsplashError):
            await tools.select_photo(context(db), payload)
    assert calls == ["/photos/photoABC"]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(EditorialAsset.id).where(
                    EditorialAsset.assignment_id == payload.assignment_id
                )
            )
            is None
        )


async def test_uncertain_tracking_is_not_replayed_in_a_new_worker(selection_pg, monkeypatch):
    sessions, context, payload, *_ = selection_pg
    calls = []

    async def request(key, path, params=None):
        calls.append(path)
        if path.endswith("/download"):
            raise UnsplashError("Transport reply lost")
        return photo()

    monkeypatch.setattr(tools, "request", request)
    for _ in range(2):
        async with sessions() as db:
            with pytest.raises(UnsplashError) as error:
                await tools.select_photo(context(db), payload)
            assert error.value.code == "unsplash_tracking_uncertain"
            assert error.value.side_effect_possible
    assert calls == ["/photos/photoABC", "/photos/photoABC/download"]
    async with sessions() as db:
        assert (await db.get(EditorialAssignment, payload.assignment_id)).editorial_version == 1
        assert (
            await db.scalars(
                select(EditorialAsset).where(EditorialAsset.assignment_id == payload.assignment_id)
            )
        ).one().status == "tracking"


@pytest.mark.parametrize("drift", ["unchanged", "rotation", "revision_only", "binding_revoked"])
async def test_gateway_approval_rechecks_unsplash_variable_revision_and_binding(
    selection_pg, monkeypatch, drift
):
    sessions, context, payload, variable, owner = selection_pg
    catalog = ToolCatalog()
    for definition, executor in tools.UNSPLASH_TOOLS:
        catalog.register(definition, executor, validator=tools.validator_for(definition.name))
    calls = []

    async def request(key, path, params=None):
        assert key == "synthetic-access-key"
        calls.append(path)
        return (
            {"url": "https://images.unsplash.com/example"}
            if path.endswith("/download")
            else photo()
        )

    monkeypatch.setattr(tools, "request", request)
    async with sessions() as db:
        ctx = context(db)
        writer = await db.get(Agent, ctx.agent_id)
        writer.approval_policy_json = [{"capability": "unsplash.*", "action": "approval"}]
        await db.commit()
        parked = await ToolGateway(ctx, catalog).request(
            "unsplash.photos.select", payload.model_dump_json(), invocation_id=new_uuid7()
        )
        assert parked.status == "needs_approval", parked.decision_reason
        approval = await db.get(Approval, parked.approval_id)
        digest = approval.action_payload_sanitized["connection_authorization_digest"]
        assert len(digest) == 64
        assert "synthetic-access-key" not in str(approval.action_payload_sanitized)
        approval.status = "approved"
        approval.decided_by_user_id = owner.id
        approval.decided_at = datetime.now(UTC)
        if drift in {"rotation", "revision_only"}:
            await VariableStore(db, ctx.crypto).set(
                VariableActor(ctx.workspace_id, "user", owner.id, is_admin=True),
                variable_id=variable.id,
                expected_version=1,
                value="rotated-synthetic-key" if drift == "rotation" else "synthetic-access-key",
            )
        elif drift == "binding_revoked":
            await db.execute(
                delete(VariableConnectionBinding).where(
                    VariableConnectionBinding.connection_id == payload.connection_id
                )
            )
        await db.commit()

    async with sessions() as db:
        outcome = await asyncio.wait_for(
            ToolGateway(context(db), catalog).resolve_approved(parked.approval_id), 10
        )
        if drift == "unchanged":
            assert outcome.status == "executed", outcome
            assert calls == ["/photos/photoABC", "/photos/photoABC/download"]
        else:
            assert outcome.status == "denied", outcome
            assert not calls
            if drift != "binding_revoked":
                assert outcome.decision_code == "approval_connection_changed"
        replay = await ToolGateway(context(db), catalog).resolve_approved(parked.approval_id)
        assert replay.replayed and replay.status == outcome.status
