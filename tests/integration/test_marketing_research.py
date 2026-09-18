"""Archive coverage and late review recovery in the isolated PostgreSQL harness."""

from importlib import import_module

import pytest

from jhin_db.models import Agent, AgentRun, Connection, Task, Workspace
from jhin_db.models.blog_corpus import BlogCorpusSync
from jhin_db.models.editorial import EditorialAssignment
from jhin_domain import new_uuid7

from . import test_company_topology_concurrency as topology_tests
from .test_company_topology_concurrency import PgDatabase

topology_database = topology_tests.topology_database

pytestmark = pytest.mark.integration


async def test_concurrent_review_result_registration_and_completion(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    continuation_cases = import_module(
        "packages.tools.tests.test_work_request_continuation_postgres"
    )
    monkeypatch.setattr(
        continuation_cases,
        "URL",
        topology_database.engine.url.render_as_string(hide_password=False),
    )
    cases = continuation_cases
    run_race = cases.test_concurrent_result_finalizers_and_registration_create_one_continuation
    await run_race()


async def test_three_minute_review_releases_capacity_and_continues_once(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporal_cases = import_module(
        "services.agent_worker.tests.test_work_request_continuation_temporal"
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL", topology_database.engine.url.render_as_string(hide_password=False)
    )
    run_late_review = (
        temporal_cases.test_late_result_runs_one_successor_after_source_releases_only_workspace_slot
    )
    await run_late_review(monkeypatch, "postgres")


async def test_4500_article_archive_coverage_and_long_body_retrieval(
    topology_database: PgDatabase,
) -> None:
    corpus_cases = import_module("packages.connectors.tests.test_ghost_corpus")
    async with topology_database.sessions() as db:
        workspace = Workspace(name="Archive acceptance", slug=f"archive-{new_uuid7().hex}")
        db.add(workspace)
        await db.flush()
        writer = Agent(workspace_id=workspace.id, name="Writer", slug="writer")
        reviewer = Agent(workspace_id=workspace.id, name="Reviewer", slug="reviewer")
        connection = Connection(
            workspace_id=workspace.id,
            name="Ghost archive",
            connector_type="ghost",
            auth_type="api_key",
        )
        db.add_all([writer, reviewer, connection])
        await db.flush()
        task = Task(
            workspace_id=workspace.id,
            title="Archive",
            assigned_agent_id=writer.id,
            correlation_id=new_uuid7(),
        )
        db.add(task)
        await db.flush()
        run = AgentRun(
            workspace_id=workspace.id, agent_id=writer.id, task_id=task.id, status="running"
        )
        assignment = EditorialAssignment(
            workspace_id=workspace.id,
            connection_id=connection.id,
            writer_agent_id=writer.id,
            publisher_agent_id=reviewer.id,
        )
        db.add_all([run, assignment])
        await db.flush()
        sync = BlogCorpusSync(
            workspace_id=workspace.id,
            connection_id=connection.id,
            assignment_id=assignment.id,
            agent_id=writer.id,
            task_id=task.id,
            run_id=run.id,
            status="running",
        )
        db.add(sync)
        await db.commit()
        await corpus_cases.test_4500_posts_require_reconciled_exact_inventory_and_keep_long_body(
            (db, sync)
        )
