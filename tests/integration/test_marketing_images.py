"""Unsplash reservation and revocation guarantees in the leased PostgreSQL harness."""

from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any

import pytest

from . import test_company_topology_concurrency as _topology
from .test_company_topology_concurrency import PgDatabase

topology_database = _topology.topology_database
image_cases = import_module("packages.connectors.tests.test_unsplash_postgres")
pytestmark = pytest.mark.integration


@pytest.fixture
async def image_selection(
    topology_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    monkeypatch.setattr(
        image_cases, "URL", topology_database.engine.url.render_as_string(hide_password=False)
    )
    async with image_cases.selection_database() as selection:
        yield selection


async def test_parallel_image_selection_once(
    image_selection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await image_cases.test_parallel_workers_reserve_one_selection_and_increment_once(
        image_selection, monkeypatch
    )


async def test_image_credential_revocation_during_metadata(
    image_selection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await image_cases.test_credential_binding_revocation_during_metadata_blocks_tracking(
        image_selection, monkeypatch
    )


async def test_uncertain_image_tracking_never_replays(
    image_selection: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await image_cases.test_uncertain_tracking_is_not_replayed_in_a_new_worker(
        image_selection, monkeypatch
    )


@pytest.mark.parametrize("drift", ["unchanged", "rotation", "revision_only", "binding_revoked"])
async def test_image_approval_checks_current_credential(
    image_selection: Any, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    await image_cases.test_gateway_approval_rechecks_unsplash_variable_revision_and_binding(
        image_selection, monkeypatch, drift
    )
