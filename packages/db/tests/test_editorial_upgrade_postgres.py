"""Opt-in historical editorial upgrade in one uniquely owned disposable database.

EDITORIAL_UPGRADE_ADMIN_URL must name the isolated localhost:55439 postgres
administration database. This test never migrates or clears the shared showcase
database. Its only DROP targets the random database it successfully created.
"""

import asyncio
import os
import re
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jhin_connectors.ghost.client import GhostApiError
from jhin_connectors.ghost.schemas import PublishInput
from jhin_connectors.ghost.tools import _publish
from jhin_db.migrate import alembic_config
from jhin_db.models import Agent, AuditEvent, Connection, GhostEditorialReview, Workspace
from jhin_domain import new_uuid7
from jhin_tools.builtin import ToolExecutionContext

ADMIN_URL = os.environ.get("EDITORIAL_UPGRADE_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="isolated editorial upgrade URL required")


async def test_0051_reviews_upgrade_preserves_history_and_denies_unbound_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = make_url(ADMIN_URL)
    assert target.host in {"127.0.0.1", "localhost"}
    assert target.port == 55439 and target.database == "postgres"
    name = f"jhin_editorial_upgrade_{new_uuid7().hex}"
    assert re.fullmatch(r"jhin_editorial_upgrade_[a-f0-9]{32}", name)
    admin = await asyncpg.connect(
        target.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    owned = False
    engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        owned = True
        url = target.set(drivername="postgresql+asyncpg", database=name).render_as_string(
            hide_password=False
        )
        config = alembic_config(url)
        await asyncio.to_thread(command.upgrade, config, "0051")
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            workspace = Workspace(name="Legacy upgrade", slug="legacy-upgrade")
            db.add(workspace)
            await db.flush()
            writer = Agent(workspace_id=workspace.id, name="Mindy", slug="mindy")
            publisher = Agent(workspace_id=workspace.id, name="Ashley", slug="ashley")
            db.add_all([writer, publisher])
            await db.flush()
            connection = Connection(
                workspace_id=workspace.id,
                name="Legacy Ghost",
                connector_type="ghost",
                auth_type="api_key",
                config_json={
                    "admin_url": "http://ghost:2368",
                    "publisher_agent_id": str(publisher.id),
                },
            )
            db.add(connection)
            await db.commit()

        async with engine.begin() as sql_db:
            legacy = await sql_db.run_sync(
                lambda sync: Table("ghost_editorial_review", MetaData(), autoload_with=sync)
            )
            for index, status in enumerate(("published", "uncertain", "approved", "pending")):
                review_id = new_uuid7()
                await sql_db.execute(
                    legacy.insert().values(
                        id=review_id,
                        workspace_id=workspace.id,
                        connection_id=connection.id,
                        post_id=f"{index:024x}",
                        revision=f"{index:064x}",
                        admin_url="http://ghost:2368",
                        provider_updated_at="2026-09-12T12:00:00.000Z",
                        snapshot_json={
                            "id": f"{index:024x}",
                            "html": f"<p>Historical {status} article with original evidence.</p>",
                            "authors": [{"id": "a" * 24, "name": "Original Author"}],
                            "feature_image_caption": "Original attribution",
                        },
                        author_agent_id=writer.id,
                        publisher_agent_id=publisher.id,
                        status=status,
                        feedback=f"Original {status} review: preserve exactly.",
                        decided_at=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
                        published_at=(
                            datetime(2026, 9, 12, 12, 1, tzinfo=UTC)
                            if status == "published"
                            else None
                        ),
                        publication_tool_call_id=new_uuid7() if status != "pending" else None,
                    )
                )
            before = {
                row["id"]: dict(row) for row in (await sql_db.execute(select(legacy))).mappings()
            }
        async with sessions() as db:
            for review_id, original in before.items():
                db.add(
                    AuditEvent(
                        workspace_id=workspace.id,
                        actor_type="agent",
                        actor_id=publisher.id,
                        action="ghost.historical_evidence",
                        target_type="ghost_editorial_review",
                        target_id=review_id,
                        metadata_json={
                            "status": original["status"],
                            "original_receipt": str(review_id),
                        },
                    )
                )
            await db.commit()
        async with engine.connect() as sql_db:
            audits_before = [
                dict(row)
                for row in (
                    await sql_db.execute(text("SELECT * FROM audit_event ORDER BY id"))
                ).mappings()
            ]

        await engine.dispose()
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as sql_db:
            assert await sql_db.scalar(text("SELECT version_num FROM alembic_version")) == (
                ScriptDirectory.from_config(config).get_current_head()
            )
            after = {
                row["id"]: dict(row)
                for row in (
                    await sql_db.execute(text("SELECT * FROM ghost_editorial_review"))
                ).mappings()
            }
            audits_after = [
                dict(row)
                for row in (
                    await sql_db.execute(text("SELECT * FROM audit_event ORDER BY id"))
                ).mappings()
            ]
        assert set(after) == set(before)
        assert audits_after == audits_before
        for review_id, original in before.items():
            expected = dict(original)
            if original["status"] in {"approved", "pending"}:
                expected["status"] = "stale"
            assert {key: after[review_id][key] for key in original} == expected
            assert after[review_id]["assignment_id"] is None
            assert after[review_id]["package_id"] is None
            assert after[review_id]["release_intent"] == "draft_only"

        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://ghost:2368")
        outbound: list[object] = []

        async def refuse_transport(*args: Any, **kwargs: Any) -> dict[str, Any]:
            outbound.append(args)
            raise AssertionError("Legacy unbound approval must never reach Ghost")

        monkeypatch.setattr("jhin_connectors.ghost.tools.ghost_request", refuse_transport)
        async with sessions() as db:
            ctx = ToolExecutionContext(
                session=db,
                workspace_id=workspace.id,
                task_id=new_uuid7(),
                run_id=new_uuid7(),
                agent_id=publisher.id,
                agent_name="Ashley",
                tool_call_id=new_uuid7(),
                session_factory=sessions,
            )
            for review_id, original in before.items():
                if original["status"] not in {"approved", "pending"}:
                    continue
                row = await db.get(GhostEditorialReview, review_id)
                assert row is not None and row.status == "stale"
                with pytest.raises(GhostApiError) as denied:
                    await _publish(
                        ctx,
                        PublishInput(connection_id=str(connection.id), review_id=str(review_id)),
                    )
                assert denied.value.code == "ghost_assignment_required"
            assert outbound == []
    finally:
        if engine is not None:
            await engine.dispose()
        if owned:
            # Identifier is generated and validated above; no shared DB name is accepted.
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()
