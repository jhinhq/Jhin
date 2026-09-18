"""Editorial guarantees in the leased live harness, on real PostgreSQL locks."""

from importlib import import_module

import pytest

from . import test_company_topology_concurrency as _topology
from .test_company_topology_concurrency import PgDatabase

topology_database = _topology.topology_database
authority_cases = import_module("packages.connectors.tests.test_editorial_authority_postgres")
draft_cases = import_module("packages.connectors.tests.test_ghost_concurrency_postgres")

pytestmark = pytest.mark.integration


async def test_installation_publisher_race(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authority_cases, "URL", topology_database.engine.url.render_as_string(hide_password=False)
    )
    await authority_cases.test_installation_publisher_claims_serialize_across_connections()


async def test_assignment_cancellation_before_queued_write(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authority_cases, "URL", topology_database.engine.url.render_as_string(hide_password=False)
    )
    await authority_cases.test_cancellation_wins_before_waiting_draft_write_dispatch(monkeypatch)


@pytest.mark.parametrize("first_tool", ["ghost.draft.create", "ghost.review.request"])
async def test_approved_draft_and_review_locks(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch, first_tool: str
) -> None:
    monkeypatch.setattr(
        draft_cases, "URL", topology_database.engine.url.render_as_string(hide_password=False)
    )
    await draft_cases.test_approved_create_and_review_wait_then_complete_once(
        monkeypatch, first_tool
    )
